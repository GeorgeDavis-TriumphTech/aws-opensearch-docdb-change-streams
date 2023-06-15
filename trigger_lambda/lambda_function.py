import json
import os
import boto3
import logging
import time

# Trigger a lambda function from within this Lambda function
def trigger_invocation_on_docdb_reader_lambda(function_name, invocation_type):

    lambdaInvokeResponse = lambda_client.invoke(
        FunctionName = function_name,
        InvocationType = invocation_type,
    )

    logger.info("Lambda Invoke Response: {}".format(lambdaInvokeResponse))

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


logger = logging.getLogger()
logger.setLevel(level = os.environ.get('LOGLEVEL', 'INFO').upper())

sns_client = boto3.client('sns')        # SNS client - for exception alerting purposes
lambda_client = boto3.client('lambda')

def lambda_handler(event, context):
    """Trigger a given Lambda function in short intervals for that Lambda function to typically compute. This is a workaround for Events Rule which cannot do trigger less than a minute"""

    logger.debug("Event: {}".format(event))
    logger.debug("Context: {}".format(context))    

    lambda_function_name = str(os.environ.get("LAMBDA_FUNCTION_NAME"))
    trigger_lambda_timeout = int(os.environ.get("TRIGGER_LAMBDA_TIMEOUT"))
    invocation_type = str(os.environ.get("INVOCATION_TYPE"))
    invocation_time_interval = int(os.environ.get("INVOCATION_TIME_INTERVAL"))

    events_processed = 0
    is_error = 0
    success_status_code_by_invocation_type = { "RequestResponse": 200, "Event": 202, "DryRun": 204 }

    try:        

        # Runs every second until the Lambda function times out. You could set a larger lambda timeout (max 15 minutes).
        iter = 0 
        while iter < (trigger_lambda_timeout / invocation_time_interval):
            logger.info("Invoking {} using AWS Request ID: {}...".format(lambda_function_name, context.aws_request_id))
            trigger_invocation_on_docdb_reader_lambda(lambda_function_name, invocation_type)
            events_processed += 1
            time.sleep(invocation_time_interval)
            iter += 1

    except Exception as ex:
        logger.error('Exception in invoking {} using AWS Request ID: {} : {}'.format(lambda_function_name, context.aws_request_id, ex))
        is_error = 1
        send_sns_alert(str(ex))
        raise

    finally:
        logger.info("{} Invocations Complete using AWS Request ID: {}".format(events_processed, context.aws_request_id))

        return {
                'statusCode': success_status_code_by_invocation_type[invocation_type],
                'description': 'Success',
                'detail': '{} records processed successfully using AWS Request ID: {}'.format(events_processed, context.aws_request_id)
            } if is_error == 0 else {
                'statusCode': 0,
                'description': 'Failure',
                'detail': 'Failed but {} records were processed successfully using AWS Request ID: {}.'.format(events_processed, context.aws_request_id)
            }