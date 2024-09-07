"""Process triton data files"""

import os
import contextlib
import tempfile
import shutil
from traceback import format_exc
import json

import imaging.imaging_maestro2_triton_root as Maestro2_Triton
import imaging.imaging_utils as imaging_utils
import azure.storage.filedatalake as azurelake
import config
import utils.dependency as deps
import time
import csv
import utils.logwatch as logging
from utils.file_map_processor import FileMapProcessor
from utils.time_estimator import TimeEstimator

from pydicom.datadict import DicomDictionary, keyword_dict


def pipeline(study_id: str):  # sourcery skip: low-code-quality
    """Process triton data files for a study
    Args:
        study_id (str): the study id
    """

    if study_id is None or not study_id:
        raise ValueError("study_id is required")

    input_folder = f"{study_id}/pooled-data/Triton"
    dependency_folder = f"{study_id}/dependency/Triton"
    pipeline_workflow_log_folder = f"{study_id}/logs/Triton"
    processed_data_output_folder = f"{study_id}/pooled-data/Triton-hpc-processed"
    ignore_file = f"{study_id}/ignore/triton.ignore"

    logger = logging.Logwatch("triton", print=True)

    # Get the list of blobs in the input folder
    file_system_client = azurelake.FileSystemClient.from_connection_string(
        config.AZURE_STORAGE_CONNECTION_STRING,
        file_system_name="stage-1-container",
    )

    # Define items as (VR, VM, description, is_retired flag, keyword)
    #   Leave is_retired flag blank.
    new_dict_items = {
        0x0022EEE0: (
            "SQ",
            "1",
            "En Face Volume Descriptor Sequence",
            "",
            "EnFaceVolumeDescriptorSequence",
        ),
        0x0022EEE1: (
            "CS",
            "1",
            "En Face Volume Descriptor Scope",
            "",
            "EnFaceVolumeDescriptorScope",
        ),
        0x0022EEE2: (
            "SQ",
            "1",
            "Referenced Segmentation Sequence",
            "",
            "ReferencedSegmentationSequence",
        ),
        0x0022EEE3: ("FL", "1", "Surface Offset", "", "SurfaceOffset"),
    }

    # Update the dictionary itself
    DicomDictionary.update(new_dict_items)

    # Update the reverse mapping from name to tag
    new_names_dict = dict([(val[4], tag) for tag, val in new_dict_items.items()])
    keyword_dict.update(new_names_dict)

    with contextlib.suppress(Exception):
        file_system_client.delete_directory(processed_data_output_folder)

    with contextlib.suppress(Exception):
        file_system_client.delete_file(f"{dependency_folder}/file_map.json")

    paths = file_system_client.get_paths(path=input_folder)

    file_paths = []

    for path in paths:
        t = str(path.name)

        original_file_name = t.split("/")[-1]

        # Check if the item is an .fda.zip file
        if not original_file_name.endswith(".zip"):
            continue

        # Get the parent folder of the file.
        # The name of this file is in the format siteName_dataType_startDate-endDate_*.fda.zip

        parts = original_file_name.split("_")

        if len(parts) != 4:
            continue
        site_name = parts[0]
        data_type = parts[1]

        start_date_end_date = parts[2]

        start_date = start_date_end_date.split("-")[0]
        end_date = start_date_end_date.split("-")[1]

        file_paths.append(
            {
                "file_path": t,
                "status": "failed",
                "processed": False,
                "site_name": site_name,
                "data_type": data_type,
                "start_date": start_date,
                "end_date": end_date,
                "organize_error": True,
                "organize_result": "",
                "convert_error": True,
                "format_error": False,
                "output_uploaded": False,
                "output_files": [],
            }
        )

    logger.debug(f"Found {len(file_paths)} files in {input_folder}")

    # Create the output folder
    file_system_client.create_directory(processed_data_output_folder)

    workflow_file_dependencies = deps.WorkflowFileDependencies()

    file_processor = FileMapProcessor(dependency_folder, ignore_file)

    total_files = len(file_paths)

    device = "Triton"

    time_estimator = TimeEstimator(len(file_paths))

    for idx, file_item in enumerate(file_paths):
        log_idx = idx + 1

        # dev
        # if log_idx == 15:
        #     break

        # Create a temporary folder on the local machine
        temp_folder_path = tempfile.mkdtemp()

        path = file_item["file_path"]

        workflow_input_files = [path]

        # get the file name from the path
        original_file_name = path.split("/")[-1]

        should_file_be_ignored = file_processor.is_file_ignored(file_item, path)

        if should_file_be_ignored:
            logger.info(f"Ignoring {original_file_name} - ({log_idx}/{total_files})")
            continue

        # get the file name from the path
        original_file_name = path.split("/")[-1]

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

        step1_folder = os.path.join(temp_folder_path, "step1")

        if not os.path.exists(step1_folder):
            os.makedirs(step1_folder)

        download_path = os.path.join(step1_folder, original_file_name)

        logger.debug(
            f"Downloading {original_file_name} to {download_path} - ({log_idx}/{total_files})"
        )

        with open(file=download_path, mode="wb") as f:
            f.write(input_file_client.download_file().readall())

        logger.info(
            f"Downloaded {original_file_name} to {download_path} - ({log_idx}/{total_files})"
        )

        zip_files = imaging_utils.list_zip_files(step1_folder)

        if len(zip_files) == 0:
            logger.warn(f"No zip files found in {step1_folder}")
            continue

        step2_folder = os.path.join(temp_folder_path, "step2")

        if not os.path.exists(step2_folder):
            os.makedirs(step2_folder)

        logger.debug(
            f"Unzipping {original_file_name} to {step2_folder} - ({log_idx}/{total_files})"
        )

        imaging_utils.unzip_fda_file(download_path, step2_folder)

        logger.info(
            f"Unzipped {original_file_name} to {step2_folder} - ({log_idx}/{total_files})"
        )

        step3_folder = os.path.join(temp_folder_path, "step3")

        step2_data_folders = imaging_utils.list_subfolders(
            os.path.join(step2_folder, device)
        )

        # process the files
        triton_instance = Maestro2_Triton.Maestro2_Triton()

        try:
            for step2_data_folder in step2_data_folders:
                organize_result = triton_instance.organize(
                    step2_data_folder, os.path.join(step3_folder, device)
                )

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

        step4_folder = os.path.join(temp_folder_path, "step4")

        if not os.path.exists(step4_folder):
            os.makedirs(step4_folder)

        protocols = [
            "triton_3d_radial_oct",
            "triton_macula_6x6_octa",
            "triton_macula_12x12_octa",
        ]

        try:
            for protocol in protocols:
                output_folder_path = os.path.join(step4_folder, device, protocol)

                if not os.path.exists(output_folder_path):
                    os.makedirs(output_folder_path)

                folders = imaging_utils.list_subfolders(
                    os.path.join(step3_folder, device, protocol)
                )

                for folder in folders:
                    triton_instance.convert(folder, output_folder_path)

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

        device_list = [os.path.join(step4_folder, device)]

        destination_folder = os.path.join(temp_folder_path, "step5")

        for device_folder in device_list:
            file_list = imaging_utils.get_filtered_file_names(device_folder)

            for file_name in file_list:

                try:
                    imaging_utils.format_file(file_name, destination_folder)
                except Exception:
                    file_item["format_error"] = True
                    logger.error(
                        f"Failed to format {file_name} - ({log_idx}/{total_files})"
                    )
                    error_exception = format_exc()
                    error_exception = "".join(error_exception.splitlines())

                    logger.error(error_exception)

                    file_processor.append_errors(error_exception, path)
                    continue

        file_item["processed"] = True

        logger.debug(
            f"Uploading outputs of {original_file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
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

                    with open(f"{full_file_path}", "rb") as data:
                        output_file_client.upload_data(data, overwrite=True)
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

    temp_folder_path = tempfile.mkdtemp()

    # Write the workflow log to a file
    timestr = time.strftime("%Y%m%d-%H%M%S")
    file_name = f"status_report_{timestr}.csv"
    workflow_log_file_path = os.path.join(temp_folder_path, file_name)

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
    deps_output = workflow_file_dependencies.write_to_file(temp_folder_path)

    json_file_path = deps_output["file_path"]
    json_file_name = deps_output["file_name"]

    logger.debug(f"Uploading dependencies to {dependency_folder}/{json_file_name}")

    with open(json_file_path, "rb") as data:
        dependency_output_file_Client = file_system_client.get_file_client(
            file_path=f"{dependency_folder}/{json_file_name}"
        )

        dependency_output_file_Client.upload_data(data, overwrite=True)

        logger.info(f"Uploaded dependencies to {dependency_folder}/{json_file_name}")

    shutil.rmtree(temp_folder_path)

    # dev
    # move the workflow log file and the json file to the current directory
    # shutil.move(workflow_log_file_path, "status.csv")
    # shutil.move(json_file_path, "file_map.json")


if __name__ == "__main__":
    pipeline("AI-READI")