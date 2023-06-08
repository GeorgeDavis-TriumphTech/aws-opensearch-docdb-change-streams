#!/bin/env python

import json
import logging
import os
import boto3
import datetime
from bson import json_util
from pymongo import MongoClient
from pymongo.errors import OperationFailure
import urllib.request
import urllib.parse

"""
Read data from a DocumentDB collection's change stream and replicate that data to MSK.

Required environment variables:
DOCUMENTDB_URI: The URI of the DocumentDB cluster to stream from.
DOCUMENTDB_SECRET: Secret Name of the credentials for the DocumentDB cluster in Secrets Manager
STATE_COLLECTION: The name of the collection in which to store sync state.
STATE_DB: The name of the database in which to store sync state.
WATCHED_COLLECTION_NAME: The name of the collection to watch for changes.
WATCHED_DB_NAME: The name of the database to watch for changes.
Iterations_per_sync: How many events to process before syncing state.
Documents_per_run: The max for the iterator loop. 
SNS_TOPIC_ARN_ALERT: The topic to send exceptions.

SNS target environment variables:
SNS_TOPIC_ARN_EVENT: The topic to send docdb events.    

S3 target environment variables:
BUCKET_NAME: The name of the bucket that will save streamed data. 
BUCKET_PATH (optional): The path of the bucket that will save streamed data.

SQS target environment variables:
SQS_QUERY_URL: The URL of the Amazon SQS queue to which a message is sent.

"""

db_client = None                        # DocumentDB client - used as source
s3_client = None                        # S3 client - used as target
sqs_client = None                       # SQS client - used as target
# SNS client - for exception alerting purposes
sns_client = boto3.client('sns')
# S3 client - used to get the DocumentDB certificates
clientS3 = boto3.resource('s3')

logger = logging.getLogger()
logger.setLevel(level = os.environ.get('LOGLEVEL', 'INFO').upper())

# The error code returned when data for the requested resume token has been deleted
TOKEN_DATA_DELETED_CODE = 136


def get_credentials():
    """Retrieve credentials from the Secrets Manager service."""
    boto_session = boto3.session.Session()

    try:
        secret_name = os.environ['DOCUMENTDB_SECRET']

        logger.info(
            'Retrieving secret {} from Secrets Manager.'.format(secret_name))

        secrets_client = boto_session.client(service_name='secretsmanager',
                                             region_name=boto_session.region_name)
        secret_value = secrets_client.get_secret_value(SecretId=secret_name)

        secret = secret_value['SecretString']
        secret_json = json.loads(secret)
        username = secret_json['username']
        password = secret_json['password']

        logger.info(
            'Secret {} retrieved from Secrets Manger.'.format(secret_name))

        return (username, password)

    except Exception as ex:
        logger.error('Failed to retrieve secret {}'.format(secret_name))
        raise


def get_db_client():
    """Return an authenticated connection to DocumentDB"""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global db_client

    if db_client is None:
        logger.info('Creating new DocumentDB client.')

        try:
            cluster_uri = os.environ['DOCUMENTDB_URI']
            (username, password) = get_credentials()          
            cluster_conn_str = 'mongodb://%s:%s@%s' % (urllib.parse.quote_plus(username), urllib.parse.quote_plus(password), cluster_uri)
            db_client = MongoClient(
                cluster_conn_str, ssl=True, retryWrites=False, tlsCAFile='rds-combined-ca-bundle.pem')
            logger.info('Successfully created new DocumentDB client.')
        except Exception as ex:
            logger.error(
                'Failed to create new DocumentDB client: {}'.format(ex))
            # send_sns_alert(str(ex))
            raise

    return db_client


def get_state_collection_client():
    """Return a DocumentDB client for the collection in which we store processing state."""

    logger.info('Creating state_collection_client.')
    try:
        db_client = get_db_client()
        state_db_name = os.environ['STATE_DB']
        state_collection_name = os.environ['STATE_COLLECTION']
        state_collection = db_client[state_db_name][state_collection_name]
    except Exception as ex:
        logger.error(
            'Failed to create new state collection client: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

    return state_collection


def get_last_processed_id():
    """Return the resume token corresponding to the last successfully processed change event."""
    last_processed_id = None
    logger.info('Returning last processed id.')
    try:
        state_collection = get_state_collection_client()
        if "WATCHED_COLLECTION_NAME" in os.environ:
            state_doc = state_collection.find_one({'currentState': True, 'dbWatched': str(os.environ['WATCHED_DB_NAME']),
                                                   'collectionWatched': str(os.environ['WATCHED_COLLECTION_NAME']), 'db_level': False})
        else:
            state_doc = state_collection.find_one({'currentState': True, 'db_level': True,
                                                   'dbWatched': str(os.environ['WATCHED_DB_NAME'])})

        if state_doc is not None:
            if 'lastProcessed' in state_doc:
                last_processed_id = state_doc['lastProcessed']
        else:
            if "WATCHED_COLLECTION_NAME" in os.environ:
                state_collection.insert_one({'dbWatched': str(os.environ['WATCHED_DB_NAME']),
                                             'collectionWatched': str(os.environ['WATCHED_COLLECTION_NAME']), 'currentState': True, 'db_level': False})
            else:
                state_collection.insert_one({'dbWatched': str(os.environ['WATCHED_DB_NAME']), 'currentState': True,
                                             'db_level': True})

    except Exception as ex:
        logger.error('Failed to return last processed id: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

    return last_processed_id


def store_last_processed_id(resume_token):
    """Store the resume token corresponding to the last successfully processed change event."""

    logger.info('Storing last processed id.')
    try:
        state_collection = get_state_collection_client()
        if "WATCHED_COLLECTION_NAME" in os.environ:
            state_collection.update_one({'dbWatched': str(os.environ['WATCHED_DB_NAME']),
                                         'collectionWatched': str(os.environ['WATCHED_COLLECTION_NAME'])}, {'$set': {'lastProcessed': resume_token}})
        else:
            state_collection.update_one({'dbWatched': str(os.environ['WATCHED_DB_NAME']), 'db_level': True, },
                                        {'$set': {'lastProcessed': resume_token}})

    except Exception as ex:
        logger.error('Failed to store last processed id: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise
    

def send_sns_alert(message):
    """send an SNS alert"""
    try:
        logger.info('Sending SNS alert.')
        response = sns_client.publish(
            TopicArn=os.environ['SNS_TOPIC_ARN_ALERT'],
            Message=message,
            Subject='Document DB Replication Alarm',
            MessageStructure='default'
        )
    except Exception as ex:
        logger.error('Exception in publishing alert to SNS: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise


def publish_sns_event(message):
    """send event to SNS"""
    try:
        logger.info('Sending SNS message event.')
        response = sns_client.publish(
            TopicArn=os.environ['SNS_TOPIC_ARN_EVENT'],
            Message=message
        )
    except Exception as ex:
        logger.error('Exception in publishing message to SNS: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise


def insertCanary():
    """Inserts a canary event for change stream activation"""

    canary_record = None

    try:
        logger.info('Inserting canary.')
        db_client = get_db_client()
        watched_db = os.environ['WATCHED_DB_NAME']

        if "WATCHED_COLLECTION_NAME" in os.environ:
            watched_collection = os.environ['WATCHED_COLLECTION_NAME']
        else:
            watched_collection = 'canary-collection'

        collection_client = db_client[watched_db][watched_collection]

        canary_record = collection_client.insert_one({"op_canary": "canary"})
        logger.info('Canary inserted.')

    except Exception as ex:
        logger.error('Exception in inserting canary: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

    return canary_record


def deleteCanary():
    """Deletes a canary event for change stream activation"""

    try:
        logger.info('Deleting canary.')
        db_client = get_db_client()
        watched_db = os.environ['WATCHED_DB_NAME']

        if "WATCHED_COLLECTION_NAME" in os.environ:
            watched_collection = os.environ['WATCHED_COLLECTION_NAME']
        else:
            watched_collection = 'canary-collection'

        collection_client = db_client[watched_db][watched_collection]
        collection_client.delete_one({"op_canary": "canary"})
        logger.info('Canary deleted.')

    except Exception as ex:
        logger.error('Exception in deleting canary: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise


def publish_sqs_event(pkey, message, order):
    """send change event to SQS minus the fullDocument"""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global sqs_client

    if sqs_client is None:
        logger.info('Creating new SQS client.')
        sqs_client = boto3.client('sqs')

    try:
        logger.info('Publishing message to SQS.')
        response = sqs_client.send_message(
            QueueUrl=os.environ['SQS_QUERY_URL'],
            MessageBody=message,
            MessageDeduplicationId=pkey,
            MessageGroupId=order
        )
    except Exception as ex:
        logger.error('Exception in publishing message to SQS: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise


def put_s3_event(event, database, collection, doc_id):
    """send full change event to S3"""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global s3_client

    if s3_client is None:
        logger.info('Creating new S3 client.')
        s3_client = boto3.client('s3')

    try:
        if "BUCKET_NAME" in os.environ:

            logger.info('Publishing message to S3.')
            s3PutObjectResponse = None
            s3ObjectKey = ""

            if "BUCKET_PATH" in os.environ:
                s3ObjectKey = str(os.environ['BUCKET_PATH']) + '/' + database + '/' + collection + '/' + datetime.datetime.now().strftime('%Y/%m/%d/') + doc_id
            else:
                s3ObjectKey = database + '/' + collection + '/' + datetime.datetime.now().strftime('%Y/%m/%d/') + doc_id
            
            s3PutObjectResponse = s3_client.put_object(
                ACL='private',
                Body=event,
                Bucket=os.environ['BUCKET_NAME'],
                Key=s3ObjectKey
            )

            if s3PutObjectResponse["ResponseMetadata"]["HTTPStatusCode"] == 200:

                logger.info('S3 PutObject Response: {}'.format(s3PutObjectResponse))

                s3MetadataDict = {}
                s3MetadataDict.update({'bucketName': os.environ['BUCKET_NAME']})
                s3MetadataDict.update({'s3ObjectKey': s3ObjectKey})
                s3MetadataDict.update({'s3ObjectVersionId': s3PutObjectResponse['VersionId']})
                s3MetadataDict.update({'database': database})
                s3MetadataDict.update({'collection': collection})
                s3MetadataDict.update({'docId': doc_id})

                logger.info('S3 Metadata: {}'.format(s3MetadataDict))
                return s3MetadataDict
            
            return None

    except Exception as ex:
        logger.error('Exception in publishing message to S3: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise


def lambda_handler(event, context):
    """Read any new events from DocumentDB and apply them to an streaming/datastore endpoint."""

    events_processed = 0
    canary_record = None
    watcher = None

    try:
        # DocumentDB watched collection set up
        db_client = get_db_client()
        watched_db = os.environ['WATCHED_DB_NAME']
        if "WATCHED_COLLECTION_NAME" in os.environ:
            watched_collection = os.environ['WATCHED_COLLECTION_NAME']
            watcher = db_client[watched_db][watched_collection]
        else:
            watcher = db_client[watched_db]
        logger.info('Watching collection {}'.format(watcher))

        # DocumentDB sync set up
        state_sync_count = int(os.environ['Iterations_per_sync'])
        last_processed_id = get_last_processed_id()
        logger.info("last_processed_id: {}".format(last_processed_id))

        with watcher.watch(full_document='updateLookup', resume_after=last_processed_id) as change_stream:
            i = 0

            if last_processed_id is None:
                canary_record = insertCanary()
                deleteCanary()

            while change_stream.alive and i < int(os.environ['Documents_per_run']):

                i += 1
                change_event = change_stream.try_next()
                logger.info('Event: {}'.format(change_event))

                if last_processed_id is None:
                    if change_event['operationType'] == 'delete':
                        store_last_processed_id(change_stream.resume_token)
                        last_processed_id = change_event['_id']['_data']
                    continue

                if change_event is None:
                    break
                else:
                    op_type = change_event['operationType']
                    op_id = change_event['_id']['_data']

                    s3MetadataDict = {}

                    if op_type in ['insert', 'update']:
                        doc_body = change_event['fullDocument']
                        doc_id = str(doc_body.pop("_id", None))
                        readable = datetime.datetime.fromtimestamp(
                            change_event['clusterTime'].time).isoformat()
                        # Uncomment the following line if you want to add operation metadata fields to the document event.
                        doc_body.update({'operation': op_type, 'timestamp': str(
                            change_event['clusterTime'].time), 'timestampReadable': str(readable)})
                        # Uncomment the following line if you want to add db and coll metadata fields to the document event.
                        # doc_body.update({'db':str(change_event['ns']['db']),'coll':str(change_event['ns']['coll'])})
                        payload = {'_id': doc_id}
                        payload.update(doc_body)
                            
                        # Publish event to SQS and message to S3
                        if "BUCKET_NAME" in os.environ and "SQS_QUERY_URL" in os.environ:

                            logger.info('S3 Payload: {}'.format(payload))

                            s3MetadataDict = put_s3_event(json_util.dumps(payload), str(
                                change_event['ns']['db']), str(change_event['ns']['coll']), doc_id)

                            if s3MetadataDict:
                                order = str(
                                change_event['ns']['db']) + '-' + str(change_event['ns']['coll'])
                            
                                change_event.pop("fullDocument", None)
                                change_event.update({"s3Metadata": s3MetadataDict})

                                logger.info('SQS Payload: {}'.format(change_event))

                                publish_sqs_event(
                                    str(doc_id), json_util.dumps(change_event), order)

                                logger.info('Processed event ID {} - doc_id {}'.format(op_id, doc_id))
                            
                            else:
                                logger.error('Error in publishing message to S3')
                                send_sns_alert('Error in publishing message to S3')
                                raise

                    if op_type == 'delete':
                        doc_id = str(change_event['documentKey']['_id'])
                        readable = datetime.datetime.fromtimestamp(
                            change_event['clusterTime'].time).isoformat()
                        payload = {'_id': doc_id}
                        # Uncomment the following line if you want to add operation metadata fields to the document event.
                        payload.update({'operation': op_type, 'timestamp': str(
                            change_event['clusterTime'].time), 'timestampReadable': str(readable)})
                        # Uncomment the following line if you want to add db and coll metadata fields to the document event.
                        # payload.update({'db':str(change_event['ns']['db']),'coll':str(change_event['ns']['coll'])})

                        # Publish event to SQS and message to S3
                        if "BUCKET_NAME" in os.environ and "SQS_QUERY_URL" in os.environ:

                            logger.info('S3 Payload: {}'.format(payload))

                            s3MetadataDict = put_s3_event(json_util.dumps(payload), str(
                                change_event['ns']['db']), str(change_event['ns']['coll']), doc_id)

                            if s3MetadataDict:
                                order = str(
                                    change_event['ns']['db']) + '-' + str(change_event['ns']['coll'])

                                logger.info('SQS Payload: {}'.format(change_event))

                                publish_sqs_event(
                                    str(doc_id), json_util.dumps(change_event), order)

                                logger.info('Processed event ID {} - doc_id {}'.format(op_id, doc_id))

                            else:
                                logger.error('Error in publishing message to S3')
                                send_sns_alert('Error in publishing message to S3')
                                raise

                    events_processed += 1

                    if events_processed >= state_sync_count and "BUCKET_NAME" not in os.environ:
                        # To reduce DocumentDB IO, only persist the stream state every N events
                        store_last_processed_id(change_stream.resume_token)
                        logger.info('Synced token {} to state collection'.format(
                            change_stream.resume_token))

    except OperationFailure as of:
        send_sns_alert(str(of))
        if of.code == TOKEN_DATA_DELETED_CODE:
            # Data for the last processed ID has been deleted in the change stream,
            # Store the last known good state so our next invocation
            # starts from the most recently available data
            store_last_processed_id(None)
        raise

    except Exception as ex:
        logger.error('Exception: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

    else:

        if events_processed > 0:

            store_last_processed_id(change_stream.resume_token)
            logger.info('Synced token {} to state collection'.format(
                change_stream.resume_token))
            return {
                'statusCode': 200,
                'description': 'Success',
                'detail': json.dumps(str(events_processed) + ' records processed successfully.')
            }
        else:
            if canary_record is not None:
                return {
                    'statusCode': 202,
                    'description': 'Success',
                    'detail': json.dumps('Canary applied. No records to process.')
                }
            else:
                return {
                    'statusCode': 201,
                    'description': 'Success',
                    'detail': json.dumps('No records to process.')
                }

    finally:
        logger.info("Processing Complete!")
