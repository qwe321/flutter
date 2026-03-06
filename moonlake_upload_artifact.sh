#!/bin/bash

FILE=$1
aws s3 cp $FILE  s3://moonlake-public-dev/$FILE --endpoint-url https://fsn1.your-objectstorage.com --profile hetzner
