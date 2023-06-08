import datetime
import cfnresponse
import logging

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

def lambda_handler(event, context):
    """
        Convert event.ResourceProperties.Seconds into a cron expression acceptable by AWS::Events::Rule
    """
    try:
        logger.debug("Event: {}".format(event))
        seconds = int(event["ResourceProperties"]["Seconds"])    

        cron_str = str(datetime.timedelta(seconds = seconds)).split(":")    
        cron_hours = cron_str[0] if cron_str[0] != '0' else '*'
        cron_minutes = cron_str[1] if cron_str[1] != '0' else '*'

        response_data = {"Value": "cron({} {} * * * ? *)".format(cron_minutes, cron_hours)}
        logger.info("Response Data: {}".format(response_data))    
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Value": "cron(0/{} {} * * ? *)".format(cron_minutes, cron_hours)}, None)

    except Exception as e:        
        logger.error(e)
        cfnresponse.send(event, context, cfnresponse.FAILED, {})