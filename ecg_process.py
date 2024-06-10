"""Process environmental sensor data files"""

import datetime
import json
import os
import tempfile
import uuid
import shutil
import ecg.ecg_root as ecg
import azure.storage.blob as azureblob
import azure.storage.filedatalake as azurelake

import config


def pipeline():
    input_folder = "AI-READI/pooled-data/ECG-pool"
    output_folder = "AI-READI/pooled-data/ECG-processed"

    sas_token = azureblob.generate_account_sas(
        account_name="b2aistaging",
        account_key=config.AZURE_STORAGE_ACCESS_KEY,
        resource_types=azureblob.ResourceTypes(container=True, object=True),
        permission=azureblob.AccountSasPermissions(read=True, write=True, list=True),
        expiry=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1),
    )

    # Get the blob service client
    blob_service_client = azureblob.BlobServiceClient(
        account_url="https://b2aistaging.blob.core.windows.net/",
        credential=sas_token,
    )

    # Get the list of blobs in the input folder
    file_system_client = azurelake.FileSystemClient.from_connection_string(
        config.AZURE_STORAGE_CONNECTION_STRING,
        file_system_name="stage-1-container",
    )

    # Delete the output folder if it exists
    try:
        file_system_client.delete_directory(output_folder)
    except:
        pass

    paths = file_system_client.get_paths(path=input_folder)

    str_paths = []

    for path in paths:
        t = str(path.name)
        str_paths.append(t)

    # Create a temporary folder on the local machine
    temp_folder_path = tempfile.mkdtemp()

    # Create the output folder
    file_system_client.create_directory(output_folder)

    for idx, path in enumerate(str_paths):

        file_client = file_system_client.get_file_client(path)
        is_directory = file_client.get_file_properties().metadata.get("hdi_isfolder")

        if is_directory:
            continue

        print(f"Processing {path} - ({idx}/{len(str_paths)})")

        # get the file name from the path
        file_name = path.split("/")[-1]

        # skip if the path is a folder (check if extension is empty)
        if not path.split(".")[-1]:
            continue

        # download the file to the temp folder
        blob_client = blob_service_client.get_blob_client(
            container="stage-1-container", blob=path
        )

        download_path = os.path.join(temp_folder_path, file_name)

        with open(download_path, "wb") as data:
            blob_client.download_blob().readinto(data)

        print(f"Downloaded {file_name} to {download_path} - ({idx}/{len(str_paths)})")

        ecg_path = download_path

        ecg_temp_folder_path = tempfile.mkdtemp()
        wfdb_temp_folder_path = tempfile.mkdtemp()

        xecg = ecg.ECG()

        conv_retval_dict = xecg.convert(
            ecg_path, ecg_temp_folder_path, wfdb_temp_folder_path
        )

        print(f"Converted {file_name} - ({idx}/{len(str_paths)})")

        output_files = conv_retval_dict["output_files"]

        print(f"Uploading {file_name} to {output_folder} - ({idx}/{len(str_paths)}")

        for file in output_files:
            with open(f"{file}", "rb") as data:
                output_blob_client = blob_service_client.get_blob_client(
                    container="stage-1-container", blob=f"{output_folder}/{file}"
                )
                output_blob_client.upload_blob(data)

        print(f"Uploaded {file_name} to {output_folder} - ({idx}/{len(str_paths)})")

        shutil.rmtree(ecg_temp_folder_path)
        shutil.rmtree(wfdb_temp_folder_path)
        os.remove(download_path)

    # write the paths to the log file
    # with open(temp_log_file_path, mode="w", encoding="utf-8") as f:
    #     formatted_text = json.dumps(extracted_metadata, indent=4)
    #     f.write(formatted_text)

    # # upload the log file to the logs folder
    # log_blob_client = blob_service_client.get_blob_client(
    #     container="stage-1-container", blob=f"{logs_folder}{workflow_id}.env.log"
    # )

    # with open(temp_log_file_path, "rb") as data:
    #     log_blob_client.upload_blob(data)


if __name__ == "__main__":
    pipeline()

    # delete the ecg.log file
    if os.path.exists("ecg.log"):
        os.remove("ecg.log")
