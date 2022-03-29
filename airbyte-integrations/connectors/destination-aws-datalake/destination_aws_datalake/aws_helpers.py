import boto3
import enum
from botocore.exceptions import ClientError
from retrying import retry

from .config_reader import ConnectorConfig

from airbyte_cdk.destinations import Destination


class AuthMode(enum.Enum):
    IAM_ROLE="ROLE"
    IAM_USER="USER"


class AwsHelper:
    COLUMNS_MAPPING = {"number": "float", "string": "string", "integer": "int"}

    def __init__(self, connector_config, destination: Destination):
        self._connector_config: ConnectorConfig = connector_config
        self._destination: Destination = destination
        self._bucket_name = connector_config.BucketName
        self.logger = self._destination.logger

        # IAM session
        self.create_session()

        # S3 client
        self.s3_client = self.session.client("s3", region_name=connector_config.Region)

        # Glue client
        self.glue_client = self.session.client("glue")

        # LakeFormation
        self.lf_client = self.session.client("lakeformation")

    # IAM
    @retry(stop_max_attempt_number=10, wait_random_min=1000, wait_random_max=2000)
    def create_session(self):
        if self._connector_config.AuthMode == AuthMode.IAM_USER.value:
            self._session = boto3.Session(
                aws_access_key_id=self._connector_config.AccessKeyId,
                aws_secret_access_key=self._connector_config.SecretAccessKey,
                region_name=self._connector_config.Region,
            )
        elif self._connector_config.AuthMode == AuthMode.IAM_ROLE.value:
            sts = boto3.client("sts")
            creds = sts.assume_role(
                RoleArn=self._connector_config.RoleArn,
                RoleSessionName="airbyte-destination-aws-datalake",
            )
            self._session = boto3.Session(
                aws_access_key_id=creds.get("Credentials", {}).get("AccessKeyId", "-"),
                aws_secret_access_key=creds.get("Credentials", {}).get(
                    "SecretAccessKey", "-"
                ),
                aws_session_token=creds.get("Credentials", {}).get("SessionToken", "-"),
                region_name=self._connector_config.Region,
            )

    @property
    def session(self) -> boto3.Session:
        return self._session

    # S3
    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def head_bucket(self):
        self.s3_client.head_bucket(Bucket=self._bucket_name)

    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def head_object(self, object_key):
        res = self.s3_client.head_object(Bucket=self._bucket_name, Key=object_key)
        return res

    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def put_object(self, object_key, body):
        res = self.s3_client.put_object(
            Bucket=self._bucket_name, Key=object_key, Body="\n".join(body)
        )

    @staticmethod
    def batch_iterate(iterable, n = 1):
        l = len(iterable)
        for ndx in range(0, l, n):
            yield iterable[ndx:min(ndx + n, l)]

    # Glue
    def get_table(self, txid, database_name: str, table_name: str, location: str):
        table = None
        try:
            table = self.glue_client.get_table(DatabaseName=database_name, Name=table_name, TransactionId=txid)
        except ClientError as e:
            if e.response["Error"]["Code"] == "EntityNotFoundException":
                table_input = {
                    "Name": table_name,
                    "TableType": "GOVERNED",
                    "StorageDescriptor": {
                        "Location": location,
                        "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                        "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                        "SerdeInfo": {
                            "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe",
                            "Parameters": {
                                "paths": ","
                            }
                        },
                    },
                    "PartitionKeys": [],
                    "Parameters": {
                        "classification": "json",
                        "lakeformation.aso.status": "true"
                    },
                }
                self.glue_client.create_table(
                    DatabaseName=database_name,
                    TableInput=table_input,
                    TransactionId=txid
                )
                table = self.glue_client.get_table(DatabaseName=database_name, Name=table_name, TransactionId=txid)
            else:
                err = e.response["Error"]["Code"]
                self.logger.error(f"An error occurred: {err}")
                raise
        if table:
            return table
        else:
            return None

    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def update_table(self, database, table_info, transaction_id):
        self.glue_client.update_table(
            DatabaseName=database, TableInput=table_info, TransactionId=transaction_id
        )

    def preprocess_type(self, property_type):
        if type(property_type) is list:
            not_null_types = list(filter(lambda t : t != "null", property_type))
            if len(not_null_types) > 2:
                return "string"
            else:
                return not_null_types[0]
        else:
            return property_type

    def update_table_schema(self, txid, database, table, schema):
        table_info = table["Table"]
        table_info_keys = list(table_info.keys())
        for k in table_info_keys:
            if k not in [
                "Name",
                "Description",
                "Owner",
                "LastAccessTime",
                "LastAnalyzedTime",
                "Retention",
                "StorageDescriptor",
                "PartitionKeys",
                "ViewOriginalText",
                "ViewExpandedText",
                "TableType",
                "Parameters",
                "TargetTable",
                "IsRowFilteringEnabled",
            ]:
                table_info.pop(k)

        self.logger.info("Schema = " + repr(schema))

        columns = [
            {"Name": k, "Type": self.COLUMNS_MAPPING.get(self.preprocess_type(v["type"]), v["type"])}
            for (k, v) in schema.items()
        ]
        if "StorageDescriptor" in table_info:
            table_info["StorageDescriptor"]["Columns"] = columns
        else:
            table_info["StorageDescriptor"] = {"Columns": columns}
        self.update_table(database, table_info, txid)
        self.glue_client.update_table(
            DatabaseName=database, TableInput=table_info, TransactionId=txid
        )

    # LakeFormation
    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def get_all_table_objects(self, txid, database, table):
        table_objects = []

        try:
            res = self.lf_client.get_table_objects(
                DatabaseName=database, TableName=table, TransactionId=txid
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "EntityNotFoundException":
                return []
            else:
                err = e.response["Error"]["Code"]
                self.logger.error(f"Could not get table objects due to error: {err}")
                raise

        while True:
            next_token = res.get("NextToken", None)
            partition_objects = res.get("Objects")
            table_objects.extend([p["Objects"] for p in partition_objects])
            if next_token:
                res = self.lf_client.get_table_objects(
                    DatabaseName=database,
                    TableName=table,
                    TransactionId=txid,
                    NextToken=next_token,
                )
            else:
                break
        flat_list = [item for sublist in table_objects for item in sublist]
        return flat_list

    @retry(stop_max_attempt_number=10, wait_random_min=2000, wait_random_max=3000)
    def purge_table(self, txid, database, table):
        self.logger.debug(f"Going to purge table {table}")
        write_ops = []
        all_objects = self.get_all_table_objects(txid, database, table)
        write_ops.extend([{"DeleteObject": {"Uri": o["Uri"]}} for o in all_objects])
        if len(write_ops) > 0:
            self.logger.debug(f"{len(write_ops)} objects to purge")
            for batch in self.batch_iterate(write_ops, 99):
              self.logger.debug(f"Purging batch")
              try:
                self.lf_client.update_table_objects(
                    TransactionId=txid,
                    DatabaseName=database,
                    TableName=table,
                    WriteOperations=batch,
                )
              except ClientError as e:
                self.logger.error(f"Could not delete object due to exception {repr(e)}")
                raise
        else:
            self.logger.debug("Table was empty, nothing to purge.")

    def update_governed_table(
            self, txid, database, table, bucket, object_key, etag, size
    ):
        self.logger.debug(f"Updating governed table {database}:{table}")
        write_ops = [
            {
                "AddObject": {
                    "Uri": f"s3://{bucket}/{object_key}",
                    "ETag": etag,
                    "Size": size,
                }
            }
        ]

        self.lf_client.update_table_objects(
            TransactionId=txid,
            DatabaseName=database,
            TableName=table,
            WriteOperations=write_ops,
        )


class LakeformationTransaction:
    def __init__(self, aws_helper: AwsHelper):
        self._aws_helper = aws_helper
        self._transaction = None
        self._logger = aws_helper.logger

    @property
    def txid(self):
        return self._transaction["TransactionId"]

    def cancel_transaction(self):
        self._logger.debug("Canceling Lakeformation Transaction")
        self._aws_helper.lf_client.cancel_transaction(TransactionId=self.txid)

    def commit_transaction(self):
        self._logger.debug("Commiting Lakeformation Transaction")
        self._aws_helper.lf_client.commit_transaction(TransactionId=self.txid)

    def extend_transaction(self):
        self._logger.debug("Extending Lakeformation Transaction")
        self._aws_helper.lf_client.extend_transaction(TransactionId=self.txid)

    def __enter__(self, transaction_type="READ_AND_WRITE"):
        self._logger.debug("Starting Lakeformation Transaction")
        self._transaction = self._aws_helper.lf_client.start_transaction(TransactionType=transaction_type)
        self._logger.debug(f"Transaction id = {self.txid}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logger.debug("Exiting LakeformationTransaction context manager")
        if exc_type:
            self._logger.error("Exiting LakeformationTransaction context manager due to an exception")
            self._logger.error(repr(exc_type))
            self._logger.error(repr(exc_val))
            self.cancel_transaction()
            self._transaction = None
        else:
            self._logger.debug("Exiting LakeformationTransaction context manager due to reaching end of with block")
            self.commit_transaction()
            self._transaction = None
