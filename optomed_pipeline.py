"""Process ecg data files"""

import os
import tempfile
import shutil

import imaging.imaging_optomed_retinal_photography_root as Optomed
import imaging.imaging_utils as imaging_utils
import azure.storage.filedatalake as azurelake
import config
import utils.dependency as deps
import time
import csv
import utils.logwatch as logging
from utils.file_map_processor import FileMapProcessor
from utils.time_estimator import TimeEstimator
from traceback import format_exc
import json


def pipeline(study_id: str):  # sourcery skip: low-code-quality
    """Process ecg data files for a study
    Args:
        study_id (str): the study id
    """

    if study_id is None or not study_id:
        raise ValueError("study_id is required")

    input_folder = f"{study_id}/pooled-data/Optomed"
    dependency_folder = f"{study_id}/dependency/Optomed"
    pipeline_workflow_log_folder = f"{study_id}/logs/Optomed"
    processed_data_output_folder = f"{study_id}/pooled-data/Optomed-processed"
    ignore_file = f"{study_id}/ignore/optomed.ignore"

    logger = logging.Logwatch("optomed", print=True)

    # Get the list of blobs in the input folder
    file_system_client = azurelake.FileSystemClient.from_connection_string(
        config.AZURE_STORAGE_CONNECTION_STRING,
        file_system_name="stage-1-container",
    )

    paths = file_system_client.get_paths(path=input_folder)

    file_paths = []

    for path in paths:
        t = str(path.name)

        file_name = t.split("/")[-1]

        # Check if the item is an dicom file
        if file_name.split(".")[-1] != "dcm":
            continue

        # Get the parent folder of the file.
        # The name of this folder is in the format siteName_dataType_startDate-endDate
        batch_folder = t.split("/")[-2]

        # Check if the folder name is in the format siteName_dataType_startDate-endDate
        if len(batch_folder.split("_")) != 3:
            continue

        site_name, data_type, start_date_end_date = batch_folder.split("_")

        start_date = start_date_end_date.split("-")[0]
        end_date = start_date_end_date.split("-")[1]

        file_paths.append(
            {
                "file_path": t,
                "status": "failed",
                "processed": False,
                "batch_folder": batch_folder,
                "site_name": site_name,
                "data_type": data_type,
                "start_date": start_date,
                "end_date": end_date,
                "organize_error": True,
                "organize_result": "",
                "convert_error": True,
                "format_error": True,
                "output_uploaded": False,
                "output_files": [],
            }
        )

    logger.debug(f"Found {len(file_paths)} files in {input_folder}")

    # Create the output folder
    file_system_client.create_directory(processed_data_output_folder)

    # Create a temporary folder on the local machine
    meta_temp_folder_path = tempfile.mkdtemp()

    file_processor = FileMapProcessor(dependency_folder, ignore_file)

    workflow_file_dependencies = deps.WorkflowFileDependencies()

    total_files = len(file_paths)

    time_estimator = TimeEstimator(len(file_paths))

    for idx, file_item in enumerate(file_paths):
        log_idx = idx + 1

        # dev
        # if log_idx == 10:
        #     break

        # Create a temporary folder on the local machine
        temp_folder_path = tempfile.mkdtemp()

        path = file_item["file_path"]

        workflow_input_files = [path]

        # get the file name from the path
        file_name = path.split("/")[-1]

        should_file_be_ignored = file_processor.is_file_ignored(file_item, path)

        if should_file_be_ignored:
            logger.info(f"Ignoring {file_name} - ({log_idx}/{total_files})")
            continue

        # download the file to the temp folder
        input_file_client = file_system_client.get_file_client(file_path=path)

        input_last_modified = input_file_client.get_file_properties().last_modified

        should_process = file_processor.file_should_process(path, input_last_modified)

        if not should_process:
            logger.time(time_estimator.step())
            logger.debug(
                f"The file {path} has not been modified since the last time it was processed",
            )
            logger.debug(
                f"Skipping {path} - ({log_idx}/{total_files}) - File has not been modified"
            )

            continue

        file_processor.add_entry(path, input_last_modified)

        file_processor.clear_errors(path)

        logger.debug(f"Processing {path} - ({log_idx}/{total_files})")

        # get the file name from the path
        original_file_name = path.split("/")[-1]

        step1_folder = os.path.join(temp_folder_path, "step1")

        if not os.path.exists(step1_folder):
            os.makedirs(step1_folder)

        download_path = os.path.join(step1_folder, original_file_name)

        with open(file=download_path, mode="wb") as f:
            f.write(input_file_client.download_file().readall())

        logger.info(
            f"Downloaded {file_name} to {download_path} - ({log_idx}/{total_files})"
        )

        filtered_file_names = imaging_utils.get_filtered_file_names(step1_folder)

        step2_folder = os.path.join(temp_folder_path, "step2")

        optomed_instance = Optomed.Optomed()

        logger.debug(f"Organizing {original_file_name} - ({log_idx}/{total_files})")

        try:
            for file in filtered_file_names:

                organize_result = optomed_instance.organize(file, step2_folder)

                file_item["organize_result"] = json.dumps(organize_result)
        except Exception:
            logger.error(
                f"Failed to organize {original_file_name} - ({log_idx}/{total_files})"
            )
            error_exception = format_exc()
            error_exception = "".join(error_exception.splitlines())

            logger.error(error_exception)

            file_processor.append_errors(error_exception, path)
            continue

        file_item["organize_error"] = False

        step3_folder = os.path.join(temp_folder_path, "step3")

        protocols = ["optomed_mac_or_disk_centered_cfp"]

        try:
            for protocol in protocols:
                output = os.path.join(step3_folder, protocol)

                if not os.path.exists(output):
                    os.makedirs(output)

                files = imaging_utils.get_filtered_file_names(
                    os.path.join(step2_folder, protocol)
                )

                for file in files:
                    optomed_instance.convert(file, output)
        except Exception:
            logger.error(
                f"Failed to convert {original_file_name} - ({log_idx}/{total_files})"
            )
            error_exception = format_exc()
            error_exception = "".join(error_exception.splitlines())

            logger.error(error_exception)

            file_processor.append_errors(error_exception, path)
            continue

        file_item["convert_error"] = False

        device_list = [step3_folder]

        destination_folder = os.path.join(temp_folder_path, "step4")

        try:
            for folder in device_list:
                filelist = imaging_utils.get_filtered_file_names(folder)

                for file in filelist:
                    imaging_utils.format_file(file, destination_folder)
        except Exception:
            logger.error(
                f"Failed to format {original_file_name} - ({log_idx}/{total_files})"
            )
            error_exception = format_exc()
            error_exception = "".join(error_exception.splitlines())

            logger.error(error_exception)

            file_processor.append_errors(error_exception, path)
            continue

        file_item["format_error"] = False
        file_item["processed"] = True

        logger.debug(
            f"Uploading outputs of {file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
        )

        workflow_output_files = []

        outputs_uploaded = True

        file_processor.delete_preexisting_output_files(path)

        for root, dirs, files in os.walk(destination_folder):
            for file in files:
                full_file_path = os.path.join(root, file)

                f2 = full_file_path.split("/")[-5:]

                combined_file_name = "/".join(f2)

                logger.debug(
                    f"Uploading {combined_file_name} - ({log_idx}/{total_files})"
                )

                output_file_path = (
                    f"{processed_data_output_folder}/{combined_file_name}"
                )

                try:
                    output_file_client = file_system_client.get_file_client(
                        file_path=output_file_path
                    )

                    # Check if the file already exists. If it does, throw an exception
                    if output_file_client.exists():
                        raise Exception(
                            f"File {output_file_path} already exists. Throwing exception"
                        )

                    with open(full_file_path, "rb") as f:
                        output_file_client.upload_data(f, overwrite=True)

                except Exception:
                    outputs_uploaded = False
                    logger.error(
                        f"Failed to upload {combined_file_name} - ({log_idx}/{total_files})"
                    )
                    error_exception = format_exc()
                    error_exception = "".join(error_exception.splitlines())

                    logger.error(error_exception)

                    file_processor.append_errors(error_exception, path)
                    continue

                file_item["output_files"].append(output_file_path)
                workflow_output_files.append(output_file_path)

        # Add the new output files to the file map
        file_processor.confirm_output_files(
            path, workflow_output_files, input_last_modified
        )

        if outputs_uploaded:
            file_item["output_uploaded"] = True
            file_item["status"] = "success"
            logger.info(
                f"Uploaded outputs of {original_file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
            )
        else:
            logger.error(
                f"Failed to upload outputs of {original_file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
            )

        workflow_file_dependencies.add_dependency(
            workflow_input_files, workflow_output_files
        )
        logger.time(time_estimator.step())

        shutil.rmtree(temp_folder_path)

    file_processor.delete_out_of_date_output_files()

    file_processor.remove_seen_flag_from_map()

    logger.debug(f"Uploading file map to {dependency_folder}/file_map.json")

    try:
        file_processor.upload_json()
        logger.info(f"Uploaded file map to {dependency_folder}/file_map.json")
    except Exception as e:
        logger.error(f"Failed to upload file map to {dependency_folder}/file_map.json")
        raise e

    # Write the workflow log to a file
    timestr = time.strftime("%Y%m%d-%H%M%S")
    file_name = f"status_report_{timestr}.csv"
    workflow_log_file_path = os.path.join(meta_temp_folder_path, file_name)

    with open(workflow_log_file_path, mode="w") as f:
        fieldnames = [
            "file_path",
            "status",
            "processed",
            "batch_folder",
            "site_name",
            "data_type",
            "start_date",
            "end_date",
            "organize_error",
            "organize_result",
            "convert_error",
            "format_error",
            "output_uploaded",
            "output_files",
        ]

        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=",")

        for file_item in file_paths:
            file_item["output_files"] = ";".join(file_item["output_files"])

        writer.writeheader()
        writer.writerows(file_paths)

    with open(workflow_log_file_path, mode="rb") as data:
        logger.debug(
            f"Uploading workflow log to {pipeline_workflow_log_folder}/{file_name}"
        )

        workflow_output_file_Client = file_system_client.get_file_client(
            file_path=f"{pipeline_workflow_log_folder}/{file_name}"
        )

        workflow_output_file_Client.upload_data(data, overwrite=True)

        logger.info(
            f"Uploaded workflow log to {pipeline_workflow_log_folder}/{file_name}"
        )

    # Write the dependencies to a file
    deps_output = workflow_file_dependencies.write_to_file(meta_temp_folder_path)

    json_file_path = deps_output["file_path"]
    json_file_name = deps_output["file_name"]

    logger.debug(f"Uploading dependencies to {dependency_folder}/{json_file_name}")

    with open(json_file_path, "rb") as data:
        dependency_output_file_Client = file_system_client.get_file_client(
            file_path=f"{dependency_folder}/{json_file_name}"
        )

        dependency_output_file_Client.upload_data(data, overwrite=True)

        logger.info(f"Uploaded dependencies to {dependency_folder}/{json_file_name}")

    shutil.rmtree(meta_temp_folder_path)


if __name__ == "__main__":
    pipeline("AI-READI")