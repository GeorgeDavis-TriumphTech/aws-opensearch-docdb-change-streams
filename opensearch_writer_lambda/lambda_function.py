#!/bin/env python

import json
import logging
import os
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

"""
Read data from SQS, fetch S3 document, transform and pipe it to OpenSearch. Send alerts and exceptions through SNS.

Required environment variables: 
SNS_TOPIC_ARN_ALERT: The topic to send exceptions.   

SNS target environment variables:
SNS_TOPIC_ARN_EVENT: The topic to send docdb events.    

Source SQS environment variables:
SQS_QUERY_URL: The URL of the Amazon SQS queue to which a message is sent.

Source S3 environment variables:
BUCKET_NAME: The name of the bucket has the streamed data. 
BUCKET_PATH (optional): The path of the bucket has the streamed data. 

OpenSearch target environment variables:
OPENSEARCH_URI: The URI of the OpenSearch domain where data should be streamed.
"""
                                       
opensearch_client = None                # OpenSearch client - used as target
s3_client = None                        # S3 client - used as target        
sqs_client = None                       # SQS client - used as target                                                   
sns_client = boto3.client('sns')        # SNS client - for exception alerting purposes
                                  
logger = logging.getLogger()
logger.setLevel(level = os.environ.get('LOGLEVEL', 'INFO').upper())

def get_opensearch_client():
    """Return an OpenSearch client."""

    global opensearch_client

    # aws_region = os.environ.get('AWS_REGION_NAME')
    # service = 'es'
    # credentials = boto3.Session().get_credentials()
    # auth = AWSV4SignerAuth(credentials, aws_region, service)
    auth = (os.environ.get("OPENSEARCH_USER"), os.environ.get("OPENSEARCH_PASS")) # For testing only. Don't store credentials in code.

    if opensearch_client is None:
        try:            
            logger.debug('Creating OpenSearch client Amazon root CA')
            opensearch_client = OpenSearch(
                hosts=[{'host': os.environ['OPENSEARCH_URI'], 'port': 443}],
                # http_compress = True, # enables gzip compression for request bodies
                http_auth = auth,
                use_ssl=True,
                verify_certs=True,
                # connection_class = RequestsHttpConnection,
                # pool_maxsize = 20,
                ca_certs='AmazonRootCA1.pem'
            )
            return opensearch_client
        except Exception as ex:
            logger.error('Failed to create new OpenSearch client: {}'.format(ex))
            # send_sns_alert(str(ex))
            raise

def send_sns_alert(message):
    """send an SNS alert"""
    try:
        logger.debug('Sending SNS alert.')
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
        logger.debug('Sending SNS message event.')
        response = sns_client.publish(
            TopicArn=os.environ['SNS_TOPIC_ARN_EVENT'],
            Message=message
        )
    except Exception as ex:
        logger.error('Exception in publishing message to SNS: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

def get_sqs_message(event):
    """get SQS message"""
    try:
        logger.debug('Getting SQS message.')
        sqs_client = boto3.client('sqs')
        response = sqs_client.receive_message(
            QueueUrl=os.environ['SQS_QUERY_URL'],
            AttributeNames=[
                'SentTimestamp'
            ],
            MaxNumberOfMessages=1,
            MessageAttributeNames=[
                'All'
            ],
            VisibilityTimeout=0,
            WaitTimeSeconds=0
        )
        return response
    except Exception as ex:
        logger.error('Exception in getting SQS message: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

def remove_sqs_message(receipt_handle):
    """remove SQS message"""
    try:
        logger.debug('Removing SQS message - {}'.format(receipt_handle))
        sqs_client = boto3.client('sqs')
        sqs_client.delete_message(
            QueueUrl=os.environ['SQS_QUERY_URL'],
            ReceiptHandle=receipt_handle
        )
    except Exception as ex:
        logger.error('Exception in removing SQS message: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

def get_s3_object_with_version(bucket_name, bucket_path, version_id):
    """get S3 object"""

    try:
        logger.debug('Getting S3 object.')
        s3_client = boto3.client('s3')
        s3GetObjectResponse = s3_client.get_object(
            Bucket=bucket_name,
            Key=bucket_path,
            VersionId=version_id
        )

        logger.info('S3 object: {}'.format(s3GetObjectResponse))

        return s3GetObjectResponse
    
    except Exception as ex:
        logger.error('Exception in getting S3 object: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

def delete_s3_object_with_version(bucket_name, bucket_path, version_id):
    """delete S3 object"""

    try:
        logger.debug('Deleting S3 object.')
        s3_client = boto3.client('s3')
        s3_client.delete_object(
            Bucket=bucket_name,
            Key=bucket_path,
            VersionId=version_id
        )
    except Exception as ex:
        logger.error('Exception in deleting S3 object: {}'.format(ex))
        send_sns_alert(str(ex))
        raise

def lambda_handler(event, context):
    """Read any new events from DocumentDB and apply them to an streaming/datastore endpoint."""
    
    events_processed = 0

    logger.debug('Received event: {}'.format(json.dumps(event)))

    try:

        for change_event in event["Records"]:

            logger.debug('Processing change event: {}'.format(json.dumps(change_event)))

            change_event_body = json.loads(change_event['body'])

            logger.debug('change_event_body: {}'.format(change_event_body))            

            # OpenSearch target index set up
            if "OPENSEARCH_URI" in os.environ:
                    
                s3GetObjectWithVersionResponse = get_s3_object_with_version(change_event_body['s3Metadata']['bucketName'], change_event_body['s3Metadata']['s3ObjectKey'],change_event_body['s3Metadata']['s3ObjectVersionId'])

                logger.info('S3 Get Object Response: {}'.format(s3GetObjectWithVersionResponse))

                if s3GetObjectWithVersionResponse is not None and s3GetObjectWithVersionResponse["ResponseMetadata"]["HTTPStatusCode"] == 200:

                    opensearch_client = get_opensearch_client()

                    logger.debug('OpenSearch client set up.')
                    logger.debug('OpenSearch client directory: {}'.format(dir(opensearch_client)))

                    opensearch_index = str(change_event_body['ns']['db']) + '-' + str(change_event_body['ns']['coll'])
                    opensearch_doc = json.dumps(s3GetObjectWithVersionResponse["Body"].read().decode("utf-8"))

                    logger.debug('OpenSearch index: {}, docId: {}, Document: {}'.format(opensearch_index, change_event_body['s3Metadata']['docId'], opensearch_doc))

                    # logger.debug('Indexing document: {} with docId: {}'.format(opensearch_client.index(
                    #     index=opensearch_index,
                    #     id=change_event_body['s3Metadata']['docId'],
                    #     body=opensearch_doc
                    # ), change_event_body['s3Metadata']['docId']))     
                    opensearch_client.index(
                        index=opensearch_index,
                        id=change_event_body['s3Metadata']['docId'],
                        body=opensearch_doc
                    )               

                    logger.debug('Processed change event ID {}'.format(change_event_body['s3Metadata']['docId']))

                    # Delete received SQS Message
                    remove_sqs_message(change_event['receiptHandle'])

                    # Delete ingested S3 Object from versioned S3 bucket
                    delete_s3_object_with_version(change_event_body['s3Metadata']['bucketName'], change_event_body['s3Metadata']['s3ObjectKey'],change_event_body['s3Metadata']['s3ObjectVersionId'])

        events_processed += 1

    except Exception as ex:
        logger.error('Exception: {}'.format(ex))
        # send_sns_alert(str(ex))
        raise

    else:
        
        if events_processed > 0:
            return {
                'statusCode': 200,
                'description': 'Success',
                'detail': json.dumps(str(events_processed)+ ' records processed successfully.')
            }

    finally:
        logger.info("Processing Complete!")