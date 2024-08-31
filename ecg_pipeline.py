"""Process ecg data files"""

import contextlib
import os
import tempfile
import shutil
import ecg.ecg_root as ecg
import ecg.ecg_metadata as ecg_metadata
import azure.storage.filedatalake as azurelake
import config
import utils.dependency as deps
import time
import csv
from traceback import format_exc
import utils.logwatch as logging
from utils.file_map_processor import FileMapProcessor
from utils.time_estimator import TimeEstimator


def pipeline(study_id: str):  # sourcery skip: low-code-quality
    """Process ecg data files for a study
    Args:
        study_id (str): the study id
    """

    if study_id is None or not study_id:
        raise ValueError("study_id is required")

    input_folder = f"{study_id}/pooled-data/ECG"
    processed_data_output_folder = f"{study_id}/pooled-data/ECG-processed"
    dependency_folder = f"{study_id}/dependency/ECG"
    participant_filter_list_file = (
        f"{study_id}/dependency/ECG/ParticipantIDs_12_01_2023_through_07_31_2024.csv"
    )
    pipeline_workflow_log_folder = f"{study_id}/logs/ECG"
    data_plot_output_folder = f"{study_id}/pooled-data/ECG-dataplot"
    ignore_file = f"{study_id}/ignore/ecg.ignore"

    logger = logging.Logwatch("ecg", print=True)

    # Get the list of blobs in the input folder
    file_system_client = azurelake.FileSystemClient.from_connection_string(
        config.AZURE_STORAGE_CONNECTION_STRING,
        file_system_name="stage-1-container",
    )

    with contextlib.suppress(Exception):
        file_system_client.delete_directory(data_plot_output_folder)

    with contextlib.suppress(Exception):
        file_system_client.delete_directory(processed_data_output_folder)

    with contextlib.suppress(Exception):
        file_system_client.delete_file(f"{dependency_folder}/file_map.json")

    paths = file_system_client.get_paths(path=input_folder)

    file_paths = []
    participant_filter_list = []

    for path in paths:
        t = str(path.name)

        original_file_name = t.split("/")[-1]

        # Check if the item is a xml file
        if original_file_name.split(".")[-1] != "xml":
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
                "convert_error": True,
                "output_uploaded": False,
                "output_files": [],
            }
        )

    logger.debug(f"Found {len(file_paths)} files in {input_folder}")

    # Create a temporary folder on the local machine
    temp_folder_path = tempfile.mkdtemp()

    # Create the output folder
    file_system_client.create_directory(processed_data_output_folder)

    # Create a temporary folder on the local machine
    meta_temp_folder_path = tempfile.mkdtemp()

    # Get the participant filter list file
    with contextlib.suppress(Exception):
        file_client = file_system_client.get_file_client(
            file_path=participant_filter_list_file
        )

        temp_participant_filter_list_file = os.path.join(
            meta_temp_folder_path, "filter_file.csv"
        )

        with open(file=temp_participant_filter_list_file, mode="wb") as f:
            f.write(file_client.download_file().readall())

        with open(file=temp_participant_filter_list_file, mode="r") as f:
            reader = csv.reader(f)
            for row in reader:
                participant_filter_list.append(row[0])

        # remove the first row
        participant_filter_list.pop(0)

    file_processor = FileMapProcessor(dependency_folder, ignore_file)

    workflow_file_dependencies = deps.WorkflowFileDependencies()

    total_files = len(file_paths)

    manifest = ecg_metadata.ECGManifest()

    time_estimator = TimeEstimator(len(file_paths))

    for idx, file_item in enumerate(file_paths):
        log_idx = idx + 1

        # if log_idx == 5:
        #     break

        path = file_item["file_path"]

        workflow_input_files = [path]

        # get the file name from the path
        original_file_name = path.split("/")[-1]

        should_file_be_ignored = file_processor.is_file_ignored(file_item, path)

        if should_file_be_ignored:
            logger.info(f"Ignoring {original_file_name} - ({log_idx}/{total_files})")

            logger.time(time_estimator.step())
            continue

        # download the file to the temp folder
        file_client = file_system_client.get_file_client(file_path=path)

        input_last_modified = file_client.get_file_properties().last_modified

        should_process = file_processor.file_should_process(path, input_last_modified)

        if not should_process:
            logger.debug(
                f"The file {path} has not been modified since the last time it was processed",
            )
            logger.debug(
                f"Skipping {path} - ({log_idx}/{total_files}) - File has not been modified"
            )

            logger.time(time_estimator.step())
            continue

        file_processor.add_entry(path, input_last_modified)

        file_processor.clear_errors(path)

        logger.debug(f"Processing {path} - ({log_idx}/{total_files})")

        download_path = os.path.join(temp_folder_path, original_file_name)

        with open(file=download_path, mode="wb") as f:
            f.write(file_client.download_file().readall())

        logger.info(
            f"Downloaded {original_file_name} to {download_path} - ({log_idx}/{total_files})"
        )

        ecg_path = download_path

        ecg_temp_folder_path = tempfile.mkdtemp()
        wfdb_temp_folder_path = tempfile.mkdtemp()

        xecg = ecg.ECG()

        try:
            conv_retval_dict = xecg.convert(
                ecg_path, ecg_temp_folder_path, wfdb_temp_folder_path
            )

            participant_id = conv_retval_dict["participantID"]

            if participant_id not in participant_filter_list:
                logger.warn(
                    f"Participant ID {participant_id} not in the allowed list. Skipping {original_file_name} - ({log_idx}/{total_files})"
                )

                file_processor.append_errors(
                    f"Participant ID {participant_id} not in the allowed list",
                    path,
                )

                logger.time(time_estimator.step())
                continue
        except Exception:
            logger.error(
                f"Failed to convert {original_file_name} - ({log_idx}/{total_files})"
            )
            error_exception = format_exc()
            e = "".join(error_exception.splitlines())

            logger.error(e)

            file_processor.append_errors(e, path)

            logger.time(time_estimator.step())
            continue

        file_item["convert_error"] = False
        file_item["processed"] = True

        logger.debug(f"Converted {original_file_name} - ({log_idx}/{total_files})")

        output_files = conv_retval_dict["output_files"]
        participant_id = conv_retval_dict["participantID"]

        logger.debug(
            f"Uploading outputs of {original_file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
        )

        # file is in the format 1001_ecg_25aafb4b.dat

        workflow_output_files = []

        outputs_uploaded = True
        upload_exception = ""

        file_processor.delete_preexisting_output_files(path)

        for file in output_files:
            with open(f"{file}", "rb") as data:
                file_name2 = file.split("/")[-1]

                output_file_path = f"{processed_data_output_folder}/ecg_12lead/philips_tc30/{participant_id}/{file_name2}"

                try:
                    output_file_client = file_system_client.get_file_client(
                        file_path=output_file_path
                    )

                    # Check if the file already exists. If it does, throw an exception
                    if output_file_client.exists():
                        raise Exception(
                            f"File {output_file_path} already exists. Throwing exception"
                        )

                    output_file_client.upload_data(data, overwrite=True)
                except Exception:
                    logger.error(f"Failed to upload {file} - ({log_idx}/{total_files})")
                    error_exception = format_exc()
                    e = "".join(error_exception.splitlines())

                    logger.error(e)

                    outputs_uploaded = False

                    file_processor.append_errors(e, path)

                    logger.time(time_estimator.step())
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
            file_item["output_uploaded"] = upload_exception
            logger.error(
                f"Failed to upload outputs of {original_file_name} to {processed_data_output_folder} - ({log_idx}/{total_files})"
            )

        workflow_file_dependencies.add_dependency(
            workflow_input_files, workflow_output_files
        )

        # Do the data plot
        # logger.debug(f"Data plotting {original_file_name} - ({log_idx}/{total_files})")

        # dataplot_retval_dict = xecg.dataplot(conv_retval_dict, ecg_temp_folder_path)

        # logger.debug(f"Data plotted {original_file_name} - ({log_idx}/{total_files})")

        # dataplot_pngs = dataplot_retval_dict["output_files"]

        # logger.debug(
        #     f"Uploading {original_file_name} to {data_plot_output_folder} - ({log_idx}/{total_files}"
        # )

        # for file in dataplot_pngs:
        #     with open(f"{file}", "rb") as data:
        #         original_file_name = file.split("/")[-1]
        #         output_blob_client = blob_service_client.get_blob_client(
        #             container="stage-1-container",
        #             blob=f"{data_plot_output_folder}/{original_file_name}",
        #         )
        #         output_blob_client.upload_blob(data)

        # logger.debug(
        #     f"Uploaded {original_file_name} to {data_plot_output_folder} - ({log_idx}/{total_files}"
        # )

        # Create the file metadata

        # logger.debug(f"Creating metadata for {original_file_name} - ({log_idx}/{total_files})")

        # Generate the metadata

        output_hea_file = conv_retval_dict["output_hea_file"]
        output_dat_file = conv_retval_dict["output_dat_file"]

        # Check if the file already exists.
        if os.path.exists(output_hea_file) and os.path.exists(output_dat_file):
            hea_metadata = xecg.metadata(output_hea_file)

            output_hea_file = f"/cardiac_ecg/ecg_12lead/philips_tc30/{participant_id}/{output_hea_file.split('/')[-1]}"
            output_dat_file = f"/cardiac_ecg/ecg_12lead/philips_tc30/{participant_id}/{output_dat_file.split('/')[-1]}"

            manifest.add_metadata(hea_metadata, output_hea_file, output_dat_file)

        logger.debug(
            f"Metadata created for {original_file_name} - ({log_idx}/{total_files})"
        )

        logger.time(time_estimator.step())

        shutil.rmtree(ecg_temp_folder_path)
        shutil.rmtree(wfdb_temp_folder_path)
        os.remove(download_path)

    file_processor.delete_out_of_date_output_files()

    file_processor.remove_seen_flag_from_map()

    # Write the manifest to a file
    manifest_file_path = os.path.join(temp_folder_path, "manifest.tsv")

    manifest.write_tsv(manifest_file_path)

    logger.debug(f"Uploading manifest file to {dependency_folder}/manifest.tsv")

    # Upload the manifest file
    with open(manifest_file_path, "rb") as data:
        output_file_client = file_system_client.get_file_client(
            file_path=f"{processed_data_output_folder}/manifest.tsv"
        )

        output_file_client.upload_data(data, overwrite=True)

    logger.info(f"Uploaded manifest file to {dependency_folder}/manifest.tsv")

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
    workflow_log_file_path = os.path.join(temp_folder_path, file_name)

    with open(workflow_log_file_path, "w", newline="") as csvfile:
        fieldnames = [
            "file_path",
            "status",
            "processed",
            "batch_folder",
            "site_name",
            "data_type",
            "start_date",
            "end_date",
            "convert_error",
            "output_uploaded",
            "output_files",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        for file_item in file_paths:
            file_item["output_files"] = ";".join(file_item["output_files"])

        writer.writeheader()
        writer.writerows(file_paths)

    with open(workflow_log_file_path, mode="rb") as data:
        logger.debug(
            f"Uploading workflow log to {pipeline_workflow_log_folder}/{file_name}"
        )

        output_file_client = file_system_client.get_file_client(
            file_path=f"{pipeline_workflow_log_folder}/{file_name}"
        )

        output_file_client.upload_data(data, overwrite=True)

    deps_output = workflow_file_dependencies.write_to_file(temp_folder_path)

    json_file_path = deps_output["file_path"]
    json_file_name = deps_output["file_name"]

    with open(json_file_path, "rb") as data:
        output_file_client = file_system_client.get_file_client(
            file_path=f"{dependency_folder}/{json_file_name}"
        )

        output_file_client.upload_data(data, overwrite=True)

    shutil.rmtree(meta_temp_folder_path)

    # dev
    # move the workflow log file and the json file to the current directory
    # shutil.move(workflow_log_file_path, "status.csv")
    # shutil.move(json_file_path, "file_map.json")


if __name__ == "__main__":
    pipeline("AI-READI")

    # delete the ecg.log file
    if os.path.exists("ecg.log"):
        os.remove("ecg.log")