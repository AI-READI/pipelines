import logging

from pathlib import Path
import os
import pkgutil

from . import es_utils

conv_logger = logging.getLogger('es.converter')


def customize_selfdoc_header(chdr):
    '''Reads the self-documenting header template and inserts custom meta data
    Args:
        header_dict (dict): dictionary of custom header values to be inserted
    Returns:
        list of self-documenting header strings (lines)
    '''

    # Read the header template
    hdr_template_asset = 'es_doc_header.txt'
    asset_data = pkgutil.get_data(__name__, hdr_template_asset)
    hdr_template_lines = asset_data.decode('utf-8').splitlines()
    conv_logger.info(f'Header template asset {hdr_template_asset} loaded with {len(hdr_template_lines)} lines.')

    # Customize the header lines
    hdr_line_list = list()
    observation_extent = chdr['num_rows'] / (12.0 * 60 * 24)  # convert to days

    # Fill in the custom values for the header
    ch = {'# meta_sensor_id': ': ' + str(chdr['SEN55']),  # e.g. F491437702FA6836
          '# meta_participant_id': ': ' + str(chdr['pppp']),  # e.g. 9999
          '# meta_sensor_location': ': ' + chdr['location'],  # e.g. 'family room',
          '# meta_number_of_observations': ': ' + str(chdr['num_rows']),  # e.g. 175000
          '# meta_extent_of_observation_in_days': ': ' + str(round(observation_extent, 1)),  # e.g. 11.2
          '# environmental_sensor_firmware_version': ': ' + chdr['fw_version']  # e.g. 1.2.4
          }

    for line in hdr_template_lines:
        myline = line.strip().split(':')
        field = myline[0]

        if field in ch.keys():
            pline = field + ch[field]
            hdr_line_list.append(pline)
        else:
            hdr_line_list.append(line.strip())

    return hdr_line_list  # return the customized header


def pipeline_es_export(s, verbose=False):
    '''Writes out env sensor data as an enhanced csv with self-documenting header info.
    Args:
        fname (string): path and filename of where to write or append the data
        col_names (string): csv row providing the columns names
        hdr_custom_values (dict): header element keys, values used to customize the header section
    Returns:
        s (dict): status, error counts, etc.
    '''
    hdr_line_list = customize_selfdoc_header(s['r'])

    qa_complete = True  # until proven false...
    for k in s['qa'].keys():
        if s['qa'][k]['ok'] is not True:
            qa_complete = False
    if (verbose):
        print(f'DEBUG: qa_complete is {qa_complete}')

    if (s['t']['errorCount'] == 0):
        try:
            with open(s['t']['outfile_posixpath'], "w") as f:
                # write header
                for k in hdr_line_list:
                    f.write(f"{k}\n")  # \n confirm this is needed

                # write column names
                f.write(f"{s['r']['column_names']}\n")
                # TODO: check vs template

                # write rows of data
                for line in s['t']['data_list']:
                    f.write(f"{line}\n")
            s['output_file'] = str(s['t']['outfile_posixpath'])
            s['conversion_success'] = True
        except Exception as e:
            err_msg = f'Problem {e} writing the output file {s["t"]["outfile_posixpath"]}'
            conv_logger.error(err_msg)
            print(Exception)
            s['conversion_success'] = False
            s['output_file'] = None
    else:
        msg = f'Skipping final export due to {s["t"]["errorCount"]} errors for {s["t"]["input_path"]}'
        conv_logger.error(msg)
        s['conversion_success'] = False
        s['output_file'] = None

    return s


def temp_dump(str, s, verbose=False):  # remove after conversion refactor is finished
    if (verbose):
        print('-' * 40)
        print(f'Step: {str} ... s has {len(s.keys())} items.')
        print(s.keys())
        for k, v in s['t'].items():
            if k in ['data_list', 'file_list']:
                print(f'{k} ...len only: {len(v)}')
            elif k in ['column_dict', 'err_dict_from_read_files']:
                if len(list(v.keys())) > 0:
                    print(f'{k} ...{list(v.keys())[0]} {v[list(v.keys())[0]]}')
                else:
                    print(f'{k} ... dict is empty.')
            else:
                print(f'{k} ...{v}')


def convert_env_sensor(input_path, output_folder, visit_file, build_file=None, verbose=False):
    """Reads all files in input folder, checks and combines them and exports to the output_folder.
        Args:
            input_path (string): full path to folder containing input files
            output_folder (string): full path to output folder
            visit_file (string): Full path to a csv file with visit data
                if None, a single default entry is used to allow other audits to proceed
            build_file (string): Full path to a csv file with sensor ID mapped to SEN55
                Default is to use the es_sensor_id.csv included with the ES code files
        Returns:
            dict containing status, issues, and the full path to the output_file
    """
    # Default struct values enable QA to proceed where possible
    s = {
        't': {  # temporary values
            'input_path': input_path,
            'output_folder': output_folder,
            'errorCount': 0,
            'conversion_issues': [],  # includes errors and warnings

            # Remaining items are filled in with correct values as pipeline progresses

            # filled in by
            # 'pppp_fname': # value from input_path, a.k.a. pID
            # 'nnn_fname': # value from input_path

            # 'p_visit': None,  # selected items of pID's info from visit_dict

            # 'outfile_posixpath': 'TBD',  # assemble from validated info above

            # 'file_list': [],  # raw csv files

            # filled in as part of merging all csv files:
            # 'fw_version_list': [],  # list of all FW versions found in the headers of raw csv files
            # 'sen55_list': [],  # list of all SEN55 IDs found in the headers of raw csv files
            # 'column_dict':  # is this still used
            # 'data_list': [],
            # 'err_dict_from_read_files':  # is this still used

            # updated as values are fetched
        },
        'r': {  # key return values, formerly hdr_custom_values
            'fw_version': 'unknown',
            'sen55': 'NO_SEN55_IN_DICT',  # ...get_sen55_from_nnn
            'column_names': 'unknown',
            'location': 'unknown',
            'num_rows': 0,
            'pppp': 'NPID'},
        'qa': {  # definitive QA checklist and results
            # ENV-pppp-nnn foldername validation
            'pppp_fname_well_formatted': {'ok': 'TBD', 'set_by': 'unknown'},
            'nnn_fname_well_formatted': {'ok': 'TBD', 'set_by': 'unknown'},
            'pppp_in_visit_dict': {'ok': 'TBD', 'set_by': 'unknown'},
            'nnn_in_sensor_dict': {'ok': 'TBD', 'set_by': 'unknown'},
            # participant visit data valid
            'visit_date_in_study_range': {'ok': 'TBD', 'set_by': 'unknown'},
            'visit_date_before_return_date': {'ok': 'TBD', 'set_by': 'unknown'},
            'return_date_not_in_future': {'ok': 'TBD', 'set_by': 'unknown'},
            'es_data_ok': {'ok': 'TBD', 'set_by': 'unknown'},
            # csv header consistency
            'csv_sen55_all_same': {'ok': 'TBD', 'set_by': 'unknown'},
            'csv_sen55_matches_nnn_from_sensor_dict': {'ok': 'TBD', 'set_by': 'unknown'},
            'csv_fw_versions_all_same': {'ok': 'TBD', 'set_by': 'unknown'},
            'csv_col_names_all_same': {'ok': 'TBD', 'set_by': 'unknown'},
            # csv contents valid
            'csv_fname_to_first_timestamps_checked': {'ok': 'TBD', 'set_by': 'unknown'},
            # unclear what the next one meant, remove for now
            # 'csv_readable_lines_added_to_data_list': {'ok': 'TBD', 'set_by': 'unknown'},
            # 'csv_data_values_in_range': {'ok': 'TBD', 'set_by': 'unknown'},  # ToDo

            # advanced data filtering ... TBD what this may include # ToDo
            # 'data_from_visit_demo_removed': {'ok': 'TBD', 'set_by': 'unknown'},
            # 'data_from_return_device_check_removed': {'ok': 'TBD', 'set_by': 'unknown'},
            # 'qa_complete_and_successful': {'ok': 'TBD', 'set_by': 'unknown'},
        },
        # final 2 output fields will stay at the top level
        # 'conversion_success': True,  # Will be set by export() if errorCount == 0
        # 'output_file': None,  # str will be set if export (conversion) is a success
    }
    if (verbose):
        print(f'DEBUG: s[t][input_path] {s["t"]["input_path"]}')

    temp_dump('after declaration of s', s)
    # visit_dict = es_utils.build_visit_dict(visit_csv=visit_file)
    visit_dict = es_utils.build_visit_dict_rc(visit_csv=visit_file)
    esID_dict = es_utils.build_es_dict(build_csv=build_file)

    # fetch and qa the pppp and nnn extracted from input folder name
    s = es_utils.pipeline_get_pppp_nnn_from_foldername(s)
    temp_dump('after pipeline_qa_pppp_nnn', s)

    s = es_utils.pipeline_get_p_visit(s, visit_dict)
    s = es_utils.pipeline_qa_p_visit(s)
    # checked: nnn, es_data_ok, visit_date, return_date
    # add to s: p_visit [site, visit_date, return_date,
    #                    esID (same as nnn),
    #                    location, es_data_ok]
    temp_dump('after pipeline_qa_p_visit', s)

    s = es_utils.pipeline_get_sen55_from_nnn(s, esID_dict)
    # checked: nnn in dict, else use 'NO_SEN55_IN_DICT'
    # add to s: hdr_custom_values.SEN55 # TODO: why is this here, pppp not filled in?
    temp_dump('after pipeline_get_sen55_from_nnn', s)

    s = es_utils.pipeline_get_outfile_name(s)
    s = es_utils.pipeline_get_csv_list(s)
    s = es_utils.pipeline_qa_csv_fname_to_p_visit(s)  # removes files outside the window
    # add to s: outfile_posixpath, file_list
    s = es_utils.pipeline_qa_csv_namedelta_to_timestamp1(s)
    # checked: first timestamp in each file
    temp_dump('after pipeline_qa_csv_namedelta_to_timestamp1', s)

    s = es_utils.pipeline_get_merged_file_contents(s)
    # add to s: column_dict, data_list, col_name_list,
    # err_dict_from_read_files, fw_version_list, sen55_list

    # check that the raw csv files all had the same FW, SEN55 and column names
    s = es_utils.pipeline_qa_hdr_list(s, 'fw_version_list', 'fw_version', 'csv_fw_versions_all_same')  # check FW versions in the raw csv files
    s = es_utils.pipeline_qa_hdr_list(s, 'sen55_list', 'sen55', 'csv_sen55_all_same')  # check SEN55 IDs
    s = es_utils.pipeline_qa_hdr_list(s, 'col_name_list', 'column_names', 'csv_col_names_all_same')
    temp_dump('after pipeline_qa_header', s)

    s = es_utils.pipeline_qa_match_sen55(s)
    # checked: csv sen55 matches r.SEN55
    temp_dump('after pipeline_qa_match_sen55', s)

    s = es_utils.pipeline_qa_data(s)
    # add to s: hdr_custom_values.num_rows as #rows or 0
    # checked: nothing at the moment
    temp_dump('after pipeline_qa_data', s)

    # filter the rows of data to remove initial visit, return device check
    # data will be in s['t']['data_list']
    s = es_utils.pipeline_filter_visit_dates(s)

    # qa the measured values
    # s = es_utils.pipeline_qa_data_ranges(s)  # ToDo: finish implementation
    # ToDo: need to convert str to numeric to check ranges
    # temp_dump('after pipeline_qa_data_ranges', s)

    s = pipeline_es_export(s)
    # temp_dump('after pipeline_es_export', s)

    # temporary QA check:
    if (0):  # ToDo: remove after refactor is complete
        if (s['t']['errorCount'] > 0):
            print('errorCount: ', s['t']['errorCount'])
            print('conversion_issues: ', s['t']['conversion_issues'])
            print(f's keys: {s.keys()}')

            print()
            print('Final QA check: -----------------------------------')
            for k, v in s['qa'].items():
                print(k, '.....', v)
            # print(s['qa'])
            print()

    return s