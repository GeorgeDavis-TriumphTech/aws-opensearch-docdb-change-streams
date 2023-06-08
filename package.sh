SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
S3_CLOUDFORMATION_BUCKET=<YOUR_S3_CLOUDFORMATION_BUCKET>
S3_LAMBDA_BUCKET=<YOUR_S3_LAMBDA_CODE_BUCKET>

BUILD_DOCDBSQSWRITERLAMBDA=1
UPLOAD_DOCDBSQSWRITERLAMBDA=1

BUILD_OPENSEARCHWRITERLAMBDA=1
UPLOAD_OPENSEARCHWRITERLAMBDA=1

BUILD_TRIGGERLAMBDA=1
UPLOAD_TRIGGERLAMBDA=1

UPLOAD_CLOUDFORMATION=1

# Package Lambda Code
if [ $BUILD_DOCDBSQSWRITERLAMBDA -eq 1 ]; then
    cd ${SCRIPT_DIR}
    echo "\n\tBuilding docdbSqsWriterLambda"    
    mkdir -p docdb_sqs_writer_lambda && cd docdb_sqs_writer_lambda
    export APP_PATH=`pwd`
    [ -d "docdbSqsWriterLambda" ] && echo "Directory docdbSqsWriterLambda exists." && rm -rf docdbSqsWriterLambda
    python3 -m venv docdbSqsWriterLambda
    source docdbSqsWriterLambda/bin/activate
    cp ${APP_PATH}/lambda_function.py docdbSqsWriterLambda/lib/python*/site-packages/
    cp ${APP_PATH}/requirements.txt docdbSqsWriterLambda/lib/python*/site-packages/
    cp ${SCRIPT_DIR}/files/rds-combined-ca-bundle.pem docdbSqsWriterLambda/lib/python*/site-packages/
    cd docdbSqsWriterLambda/lib/python*/site-packages/
    pip3 install -r requirements.txt 
    deactivate
    mv ../dist-packages/* .
    zip -r9 ${SCRIPT_DIR}/docdbSqsWriterLambda.zip .
    rm -rf ${APP_PATH}/docdbSqsWriterLambda
fi

if [ $BUILD_OPENSEARCHWRITERLAMBDA -eq 1 ]; then
    cd ${SCRIPT_DIR}
    echo "\n\tBuilding openSearchWriterLambda"
    mkdir -p opensearch_writer_lambda && cd opensearch_writer_lambda
    export APP_PATH=`pwd`
    [ -d "openSearchWriterLambda" ] && echo "Directory openSearchWriterLambda exists." && rm -rf openSearchWriterLambda
    python3 -m venv openSearchWriterLambda
    source openSearchWriterLambda/bin/activate
    cp ${APP_PATH}/lambda_function.py openSearchWriterLambda/lib/python*/site-packages/
    cp ${APP_PATH}/requirements.txt openSearchWriterLambda/lib/python*/site-packages/
    cp ${SCRIPT_DIR}/files/AmazonRootCA1.pem openSearchWriterLambda/lib/python*/site-packages/
    cd openSearchWriterLambda/lib/python*/site-packages/
    pip3 install -r requirements.txt 
    deactivate
    mv ../dist-packages/* .
    zip -r9 ${SCRIPT_DIR}/openSearchWriterLambda.zip .
    rm -rf ${APP_PATH}/openSearchWriterLambda
fi

if [ $BUILD_TRIGGERLAMBDA -eq 1 ]; then
    cd ${SCRIPT_DIR}
    echo "\n\tBuilding triggerLambda"
    mkdir -p trigger_lambda && cd trigger_lambda
    export APP_PATH=`pwd`
    [ -d "triggerLambda" ] && echo "Directory triggerLambda exists." && rm -rf triggerLambda
    python3 -m venv triggerLambda
    source triggerLambda/bin/activate
    cp ${APP_PATH}/lambda_function.py triggerLambda/lib/python*/site-packages/
    cp ${APP_PATH}/requirements.txt triggerLambda/lib/python*/site-packages/
    cp ${SCRIPT_DIR}/files/AmazonRootCA1.pem triggerLambda/lib/python*/site-packages/
    cd triggerLambda/lib/python*/site-packages/
    pip3 install -r requirements.txt 
    deactivate
    mv ../dist-packages/* .
    zip -r9 ${SCRIPT_DIR}/triggerLambda.zip .
    rm -rf ${APP_PATH}/triggerLambda
fi

# Upload code to Amazon S3
cd ${SCRIPT_DIR}

if [ $UPLOAD_DOCDBSQSWRITERLAMBDA -eq 1 ]; then
    echo "\n\tUploading docdbSqsWriterLambda"
    aws s3 cp docdbSqsWriterLambda.zip s3://${S3_LAMBDA_BUCKET}/docdbSqsWriterLambda.zip
fi

if [ $UPLOAD_OPENSEARCHWRITERLAMBDA -eq 1 ]; then
    echo "\n\tUploading openSearchWriterLambda"
    aws s3 cp openSearchWriterLambda.zip s3://${S3_LAMBDA_BUCKET}/openSearchWriterLambda.zip
fi 

if [ $UPLOAD_TRIGGERLAMBDA -eq 1 ]; then
    echo "\n\tUploading triggerLambda"
    aws s3 cp triggerLambda.zip s3://${S3_LAMBDA_BUCKET}/triggerLambda.zip
fi 

if [ $UPLOAD_CLOUDFORMATION -eq 1 ]; then
    echo "\n\tUploading cloudformation templates"
    aws s3 cp cloudformation/docdb_change_streams_v2.yml s3://${S3_CLOUDFORMATION_BUCKET}/docdb_change_streams_v2.yml
fi

echo "\nFinished at: `date`\n"