#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
========================================================================================================================
    Script to prepare and process Swedish APC data files
    Ulf Kronman 2017-04-06
    Adapted from and based on code by Christoph Broschinski, Copyright (c) 2016

    ToDo
    -----
    Handle commented lines in TSV input files
    Handle files with only 6 mandatory fields
    Run new SLU records
    Handling of duplicate DOI's
    Error reporting module
    Clean up processing logic and introduce error handling
    Do test runs on insitutional data
    Handle duplicate entries by skipping second entry and reporting for submission to data supplier
    Report DOI errors to file(?) for correction by institutions

    Done
    -----
    2017-05-10 Run DU's records
    2017-04-11 Add final normalisation of master file before saving
    2017-04-11 Add header line to apc_se.csv output and remove header line from added data
    2017-04-07 Re-code for complete process for one APC file at a time
    2017-04-07 Do publisher normalisation here before Crossref enrichment?

========================================================================================================================
"""

import argparse
import codecs
import locale
import sys
import urllib2
import xml.etree.ElementTree as ET
from subprocess import call
from openpyxl import load_workbook
import unicodecsv as csv
import time

# Add path for script environment
# sys.path.append('/Users/ulfkro/OneDrive/KB-dokument/Open Access/Kostnader/Open APC Sweden/openapc-se')
sys.path.append('/Users/ulfkro/OneDrive/KB-dokument/Open Access/Kostnader/Open APC Sweden/openapc-se_development')

import python.openapc_toolkit as oat


# ======================================================================================================================
class Config(object):
    """ Keep configuration parameters and processes here to hide clutter from main """

    BOOL_VERBOSE = False
    BOOL_TEST = True
    INT_REPORT_WAIT = 10

    # Where do we find and put the data
    if BOOL_TEST:
        STR_DATA_DIRECTORY = '../../test/'

        STR_APC_FILE_LIST = STR_DATA_DIRECTORY + 'test_file_list.txt'

        STR_APC_SE_FILE = '../../test/test_result.csv'
    else:
        STR_DATA_DIRECTORY = '../../data/'

        STR_APC_FILE_LIST = STR_DATA_DIRECTORY + 'apc_file_list.txt'

        STR_APC_SE_FILE = '../../data/apc_se.csv'

    ARG_HELP_STRINGS = {
        "encoding": "The encoding of the CSV file. Setting this argument will " +
                    "disable automatic guessing of encoding.",
        "locale": "Set the locale context used by the script. You might want to " +
                  "set this if your system locale differs from the locale the " +
                  "CSV file was created in (Example: Using en_US as your system " +
                  "locale might become a problem if the file contains numeric " +
                  "values with ',' as decimal mark character)",
        "headers": "Ignore any CSV headers (if present) and try to determine " +
                   "relevant columns heuristically.",
        "verbose": "Be more verbose during the cleaning process.",
    }

    ERROR_MSGS = {
        "locale": "Error: Could not process the monetary value '{}' in column " +
                  "{}. This will usually have one of two reasons:\n1) The value " +
                  "does not represent a number.\n2) The value represents a " +
                  "number, but its format differs from your current system " +
                  "locale - the most common source of error will be the decimal " +
                  "mark (1234.56 vs 1234,56). Try using another locale with the " +
                  "-l option."
    }

    # # Keep a list of processed DOI's to check for duplicates - what to do if found?
    # lst_dois_processed = []

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def get_arguments(self):

        parser = argparse.ArgumentParser()
        # parser.add_argument("csv_file", help=self.ARG_HELP_STRINGS["csv_file"])
        parser.add_argument("-e", "--encoding", help=self.ARG_HELP_STRINGS["encoding"])
        parser.add_argument("-v", "--verbose", action="store_true",
                            help=self.ARG_HELP_STRINGS["verbose"])
        parser.add_argument("-l", "--locale", help=self.ARG_HELP_STRINGS["locale"])
        parser.add_argument("-i", "--ignore-header", action="store_true",
                            help=self.ARG_HELP_STRINGS["headers"])

        args = parser.parse_args()

        # If we have a request for verbose processing set config parameter to True
        if args.verbose:
            self.BOOL_VERBOSE = True

        return args
    # ------------------------------------------------------------------------------------------------------------------

# ======================================================================================================================


# ======================================================================================================================
def main():
    """ The main processing of data """

    # Create a configuration object for easier processing
    # obj_config = Config()
    # Get line arguments
    # args = obj_config.get_arguments()
    args = Config.get_arguments(Config)

    # Create a file manager object
    cob_file_manager = FileManager()

    # Open list of files to process
    lst_apc_files = cob_file_manager.get_file_list()

    # Create a data processor object
    cob_data_processor = DataProcessor()

    # Create a user interface object to interact with user
    cob_user_interface = UserInterface()

    # Process files one at a time
    for str_input_file_name in lst_apc_files:

        # Create various file names
        str_input_file_name, str_output_file_name, str_enriched_file_name = cob_file_manager.create_file_names(
            str_input_file_name)

        # Read and clean data for one file
        lst_cleaned_data = cob_data_processor.collect_apc_data(str_input_file_name, args)

        # Save the file for further processing - Write cleaned data to file
        cob_data_processor.write_cleaned_data(str_output_file_name, lst_cleaned_data)

        # Run the German enrichment process and copy files
        cob_data_processor.run_enrichment_process(str_output_file_name)

        # Copy Bielfeld out file to institution directory
        cob_file_manager.copy_enrichment_out(str_enriched_file_name)

        # Backup master file
        cob_file_manager.backup_master_file(Config.STR_APC_SE_FILE)

        # Add new enriched data to master file
        cob_data_processor.add_new_data_to_master_file(str_enriched_file_name, cob_user_interface)

    # Report errors
    if len(cob_data_processor.lst_error_messages) > 0:
        print('WARNING: There were errors during processing. Error messages list:\n')
        for str_message in cob_data_processor.lst_error_messages:
            print(str_message)
    else:
        print('INFO: No errors during processing.\n')

# ======================================================================================================================


# ======================================================================================================================
class DataProcessor(object):
    """ Data cleaning and processing """

    # Keep a list of error messages
    lst_error_messages = []

    # ------------------------------------------------------------------------------------------------------------------
    def add_new_data_to_master_file(self, str_enriched_file_name, cob_user_interface):
        """ Check how much of the newly enriched data that should be added """

        str_apc_se_file = Config.STR_APC_SE_FILE

        # Keep the header of the master file for separate writing to the final result
        lst_master_file_header = []

        # Read master file into a matrix of data - Maybe this should be a dictionary?
        dct_master_data = {}
        lst_master_dois = []
        with open(str_apc_se_file, 'rb') as csvfile:
            obj_csv_reader = csv.reader(csvfile, delimiter=',', quotechar='"')
            for lst_row in obj_csv_reader:
                str_doi = lst_row[3].lower().strip()
                if str_doi == 'doi':
                    lst_master_file_header = lst_row
                    continue
                if str_doi not in lst_master_dois and str_doi not in dct_master_data.keys():
                    lst_master_dois.append(str_doi)
                    dct_master_data[str_doi] = lst_row
                else:
                    print '!Error: Duplicate DOI {}'.format(str_doi)
        csvfile.close()

        with open(str_enriched_file_name, 'rb') as csvfile:
            obj_csv_reader = csv.reader(csvfile, delimiter=',', quotechar='"')
            for lst_row in obj_csv_reader:
                str_doi = lst_row[3].lower().strip()
                if str_doi == 'doi':
                    continue
                if str_doi not in dct_master_data.keys():
                    dct_master_data[str_doi] = lst_row
                    print(u'INFO: Added new data {}'.format(u' '.join(lst_row)))
                    continue
                else:
                    print('DOI present {}'.format(str_doi))
                    print('Present:\t{}'.format(dct_master_data[str_doi]))
                    print('New:\t\t{}'.format(lst_row))
                    if lst_row == dct_master_data[str_doi]:
                        print('INFO: Data are exactly the same. Skipping new record.')
                        continue
                    else:
                        print('Data differs. Choose item:')
                        lst_chosen_data = cob_user_interface.ask_user(dct_master_data[str_doi], lst_row)
                        dct_master_data[str_doi] = lst_chosen_data
        csvfile.close()

        # Make master dictionary to a list and sort it
        lst_master_data = [lst_row for str_doi, lst_row in dct_master_data.iteritems()]
        lst_master_data.sort()

        # Normalise names before writing to file
        lst_master_data = self.normalise_publisher_names(lst_master_data)

        # Write the new data to the master file
        print('\nINFO: Writing result to master file {}\n'.format(str_apc_se_file))
        with open(str_apc_se_file, 'wb') as csvfile:
            obj_csv_writer = csv.writer(csvfile, delimiter=',', quotechar='"')
            obj_csv_writer.writerow(lst_master_file_header)
            for lst_row in lst_master_data:
                obj_csv_writer.writerow(lst_row)
        csvfile.close()

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def normalise_publisher_names(self, lst_master_data):
        """ Final normalisation of publisher names after Crossref lookup names according to Bibsam principles """
        obj_publisher_normaliser = PublisherNormaliser()
        lst_cleaned_data = []
        for lst_row in lst_master_data:
            str_publisher_name = lst_row[5].strip()
            str_doi = lst_row[3].strip()
            if str_publisher_name:
                str_publisher_name_normalised = obj_publisher_normaliser.normalise(str_publisher_name, str_doi)
            if str_publisher_name_normalised != str_publisher_name:
                lst_row[5] = str_publisher_name_normalised
            lst_cleaned_data.append(lst_row)

        # Write new publisher names to file
        obj_publisher_normaliser.write_new_publisher_name_map()

        return lst_cleaned_data
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def run_enrichment_process(self, str_output_file_name):
        """ """

        # Run the DE process for enrichment as a shell command
        print('\nINFO: Running enrichment process on file {}'.format(str_output_file_name))
        call(["../apc_csv_processing.py", "-l", "sv_SE.UTF-8", str_output_file_name])

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def collect_apc_data(self, str_file_name, args):
        """ Method to collect data from institions suppliced CSV or TSV files """

        # A list for the cleaned data
        lst_cleaned_data = []

        print '\nINFO: Processing file: {} \n==================================================== \n'.format(
            str_file_name)

        str_input_file_name = Config.STR_DATA_DIRECTORY + str_file_name
        lst_new_apc_data = self.clean_apc_data(str_input_file_name, args)

        for lst_row in lst_new_apc_data:
            lst_cleaned_data.append(lst_row)

        return lst_cleaned_data

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def clean_apc_data(self, str_input_file, args):
        """ Process APC file """

        # Create a publisher name normalising object
        obj_publisher_normaliser = PublisherNormaliser()

        cleaned_content = []
        error_messages = []

        enc = None # CSV file encoding

        # Keep a list of processed DOI's to check for duplicates - what to do if found?
        lst_dois_processed = []

        if args.locale:
            norm = locale.normalize(args.locale)
            if norm != args.locale:
                print "locale '{}' not found, normalized to '{}'".format(
                    args.locale, norm)
            try:
                loc = locale.setlocale(locale.LC_ALL, norm)
                print "Using locale", loc
            except locale.Error as loce:
                print "Setting locale to " + norm + " failed: " + loce.message
                sys.exit()

        if args.encoding:
            try:
                codec = codecs.lookup(args.encoding)
                print ("Encoding '{}' found in Python's codec collection " +
                       "as '{}'").format(args.encoding, codec.name)
                enc = args.encoding
            except LookupError:
                print ("Error: '" + args.encoding + "' not found Python's " +
                       "codec collection. Either look for a valid name here " +
                       "(https://docs.python.org/2/library/codecs.html#standard-" +
                       "encodings) or omit this argument to enable automated " +
                       "guessing.")
                sys.exit()

        # Read file data into result dictionary object
        result = oat.analyze_csv_file(str_input_file)

        if result["success"]:
            csv_analysis = result["data"]
            print csv_analysis
        else:
            print result["error_msg"]
            sys.exit()

        if enc is None:
            enc = csv_analysis.enc

        dialect = csv_analysis.dialect
        has_header = csv_analysis.has_header

        if enc is None:
            print ("Error: No encoding given for CSV file and automated " +
                   "detection failed. Please set the encoding manually via the " +
                   "--enc argument")
            sys.exit()

        print '\nProcessing file {}'.format(str_input_file)
        csv_file = open(str_input_file, "r")

        reader = oat.UnicodeReader(csv_file, dialect=dialect, encoding=enc)

        first_row = reader.next()
        num_columns = len(first_row)
        print "\nCSV file has {} columns.".format(num_columns)

        csv_file.seek(0)
        reader = oat.UnicodeReader(csv_file, dialect=dialect, encoding=enc)

        print "\nNOTE:    *** Starting cleaning of file *** \n"

        cleaned_content = []
        error_messages = []

        row_num = 0

        for row in reader:

            row_num += 1

            # print "--- Processing line number {} ---".format(str(row_num))

            # Check input if verbose mode
            if args.verbose:
                print row

            # Skip empty lines
            if not row:
                continue

            # Skip lines without content
            if not row[0].strip():
                continue

            # Skip lines with comment sign # in first position
            if row[0] == '#':
                continue

            # Skip record if empty APC field
            if not row[3].strip():
                print '!Warning: No APC given for publication {}. Skipping entry.'.format(row[4])
                continue

            # First non-empty row should be the header
            if has_header and row_num == 1:
                header = row
                cleaned_content.append(header)
                continue

            # Put the DOI in a string for later use
            if row[3]:
                str_doi = row[3].strip()
            else:
                print 'WARNING: No DOI found'
                str_doi = ''

            current_row = []

            col_number = 0

            # Copy content of columns
            for csv_column in row:

                col_number += 1

                # Remove leading and trailing spaces
                csv_column = csv_column.strip()

                if csv_column.lower() == u'sant':
                    csv_column = u'TRUE'
                elif csv_column.lower() == u'falskt':
                    csv_column = u'FALSE'
                elif csv_column == u'true':
                    csv_column = u'TRUE'
                elif csv_column == u'false':
                    csv_column = u'FALSE'

                # Handling of APC column
                if col_number == 3:

                    # Clean monetary Euro column from spaces due to formatting
                    csv_column = ''.join(csv_column.split())

                    # Change commas to periods
                    csv_column = csv_column.replace(",", ".")

                # Check for DOI duplicates
                if col_number == 4:
                    if csv_column in lst_dois_processed:
                        print '!Error duplicate DOI {} - Org: {} - Year: {} '.format(
                            csv_column, row[0], row[1]
                        )
                        sys.exit()
                    else:
                        lst_dois_processed.append(csv_column)

                # Publisher name normalisation, use map or send DOI for CrossRef lookup
                if col_number == 6 and csv_column:
                    str_publisher_name_normalised = obj_publisher_normaliser.normalise(csv_column, str_doi)
                    csv_column = str_publisher_name_normalised

                if csv_column != 'None':
                    current_row.append(csv_column)
                else:
                    current_row.append('')

            # Check output if verbose mode
            if args.verbose:
                print current_row

            cleaned_content.append(current_row)

        csv_file.close()

        if not error_messages:
            oat.print_g("Metadata cleaning successful, no errors occured\n")
        else:
            oat.print_r("There were errors during the cleaning process:\n")
            for msg in error_messages:
                print msg + "\n"

        # Write new publisher names to file
        obj_publisher_normaliser.write_new_publisher_name_map()

        return cleaned_content

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def write_cleaned_data(self, str_output_file_name, lst_cleaned_content):

        print 'INFO: Writing result to file {}'.format(str_output_file_name)

        with open(str_output_file_name, 'w') as out:

            for lst_line in lst_cleaned_content:
                if Config.BOOL_VERBOSE:
                    print lst_line
                if lst_line:
                    out.write(u'\t'.join(lst_line).encode("utf-8"))
                    out.write(u'\n')
    # ------------------------------------------------------------------------------------------------------------------


# ======================================================================================================================


# ======================================================================================================================
class FileManager(object):
    """ Class to keep file managing parameters and methods """

    # ------------------------------------------------------------------------------------------------------------------
    # @staticmethod
    def get_file_list(self):
        """ Fetch list of APC files to process
        :return: List of file names, including org directories
        """

        str_file_list_file = Config.STR_APC_FILE_LIST
        lst_apc_files = []
        try:
            fp_apc_files = open(str_file_list_file, 'r')
            print '\n--------------------------------------'
            print 'INFO: Processing files:'
            for str_line in fp_apc_files:
                # Don't process if we have a comment (#) on the line
                if '#' in str_line:
                    continue
                lst_apc_files.append(str_line.strip())
                print str_line.strip()
            print '--------------------------------------\n'
        except IOError:
            print 'File list not found in: {}'.format(str_file_list_file)
            sys.exit()

        return lst_apc_files

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def create_file_names(self, str_input_file_name):
        """ Create names for various files """

        str_output_file_name = ''

        # Create an output file name
        if r'.csv' in str_input_file_name:
            str_output_file_name = Config.STR_DATA_DIRECTORY + str_input_file_name.replace(r'.csv', r'_cleaned.tsv')
        elif r'.tsv' in str_input_file_name:
            str_output_file_name = Config.STR_DATA_DIRECTORY + str_input_file_name.replace(r'.tsv', r'_cleaned.tsv')
        elif r'.xlsx' in str_input_file_name:
            str_output_file_name = Config.STR_DATA_DIRECTORY + str_input_file_name.replace(r'.xlsx', r'_cleaned.tsv')
            str_input_file_name = self.convert_excel_to_tsv(str_input_file_name)
        else:
            sys.exit('!Error: File {} is not in proper format for processing'.format(str_input_file_name))

        # Create a name for the final enriched file
        str_enriched_file_name = str_output_file_name.replace('_cleaned.tsv', '_enriched.csv')

        return str_input_file_name, str_output_file_name, str_enriched_file_name

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def convert_excel_to_tsv(self, str_excel_file):
        """ If we have an Excel file as input, convert it to TSV for OA toolkit processing """

        # Make a TSV name to return to caller
        str_tsv_file = str_excel_file.replace(r'.xlsx',r'.tsv')

        str_excel_dir_file = Config.STR_DATA_DIRECTORY + str_excel_file
        str_tsv_dir_file = Config.STR_DATA_DIRECTORY + str_tsv_file
        # fp_tsv_file = open(str_tsv_dir_file, 'w')

        wb = load_workbook(filename=str_excel_dir_file, read_only=True)
        ws = wb.active  # ['Blad1']

        mx_converted_data = []

        for row in ws.rows:
            lst_row = []
            col_number = 0
            for cell in row:
                col_number += 1
                if col_number > 11:
                    break
                if cell.value != 'None':
                    lst_row.append(unicode(cell.value))
                else:
                    lst_row.append('')
            # print lst_row
            mx_converted_data.append(lst_row)

            # fp_tsv_file.write(u'\t'.join(lst_row).encode("utf-8"))
            # fp_tsv_file.write('\n')

        with open(str_tsv_dir_file, 'wb') as csvfile:
            obj_tsv_writer = csv.writer(csvfile, delimiter='\t', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            for lst_row in mx_converted_data:
                obj_tsv_writer.writerow(lst_row)

        return str_tsv_file
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def backup_master_file(self, str_apc_se_file):
        """ Make a backup of master file before processing it
        :param str_apc_se_file:
        :return:
        """

        str_apc_se_backup = str_apc_se_file.replace(r'_se.csv', r'_se_backup.csv')  # ../../data/apc_se_backup.csv'
        print('\nINFO: Making a backup copy of master file: {}\n'.format(str_apc_se_backup))
        call(['cp', str_apc_se_file, str_apc_se_backup])

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def copy_enrichment_out(self, str_enriched_file_name):
        """ Copy the output from python/se/out.csv to the organisation directory """

        print('\nCopying python/se/out.csv to {}'.format(str_enriched_file_name))
        call(["cp", 'out.csv', str_enriched_file_name])
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def remove_cleaned_file(self, str_cleaned_file_name):
        """ Remove the temporary cleaned file
        :param str_cleaned_file_name:
        :return:
        """
        print('\nRemoving temporary file {}'.format(str_cleaned_file_name))
        call(["rm", str_cleaned_file_name])

    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def convert_excel_ssv_to_csv(self, str_apc_se_file):
        """ Convert Excel funny semicolon-separated CSV to a proper CSV """

        mx_master_data = []
        with open(str_apc_se_file, 'rb') as csvfile:
            obj_csv_reader = csv.reader(csvfile, delimiter=',', quotechar='"')
            for lst_row in obj_csv_reader:
                mx_master_data.append(lst_row)
        csvfile.close()

        with open(str_apc_se_file, 'wb') as fp_csv_file:
            obj_csv_witer = csv.writer(fp_csv_file, delimiter=',', quotechar='"')
            for lst_row in mx_master_data:
                obj_csv_witer.writerow(lst_row)

        fp_csv_file.close()

    # ------------------------------------------------------------------------------------------------------------------

# ======================================================================================================================


# ======================================================================================================================
class UserInterface(object):
    """ Class for methods for interacting with user """

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self):
        """ """
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def print_record_number(self, int_record_count):
        """ """
        print 'Record: {}'.format(int_record_count)
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def report(self, obj_input_data, obj_publication=None, str_reason=''):
        """ """
        print str_reason
        print obj_input_data.__unicode__()
        if obj_publication:
            print obj_publication.__unicode__()
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def report_and_wait(self, obj_input_data, obj_publication=None, str_reason=''):
        """ """
        print str_reason
        print obj_input_data.__unicode__()
        if obj_publication:
            print obj_publication.__unicode__()
        time.sleep(Config.INT_REPORT_WAIT)
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def report_and_stop(self, obj_input_data, obj_publication=None, str_reason=''):
        """ """
        print str_reason
        print obj_input_data.__unicode__()
        if obj_publication:
            print obj_publication.__unicode__()
        sys.exit('Stopping after report')
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def report_input(self, obj_output_data):
        """ Put input data into a dictionary and report it to user
        :param lst_row: List with input data
        :return: dct_input_data: Dictionary with input data
        """
        # Print neat divider
        self.print_divider(u'Input data')
        # print(lst_row)
        print(obj_output_data.__unicode__())
        # Print neat divider
        self.print_divider(u'End of record')
        print(u'')
        return obj_output_data
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def ask_user(self, lst_present_publication, lst_new_publication):
        """ Ask opinion from user and return choice """

        print 'NOTE: Several name choices found. Please chose one alternative.'
        print 'Present:\t1) {}'.format(' '.join(lst_present_publication))
        print 'New:\t\t2) {}'.format(' '.join(lst_new_publication))
        str_choice = raw_input('Choose 1 or [2]:  ')
        if str_choice == '1':
            lst_chosen_data = lst_present_publication
        elif str_choice == '2':
            lst_chosen_data = lst_new_publication
        else:
            lst_chosen_data = lst_new_publication
        return lst_chosen_data
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def print_divider(str_message, bool_space_before=False, bool_space_after=False):
        """ Print a neat divider betweeen routines
        :param str_message: The message to be encapsulated in the divider
                bool_space_before = True if extra spacing before divider
                bool_space_after = True if extra spacing after divider
        :return: None
        """
        if bool_space_before:
            print
        print(u'---[{}]---------------------------------------------------------------------------'.format(str_message))
        if bool_space_after:
            print
    # ------------------------------------------------------------------------------------------------------------------

# ======================================================================================================================


# ======================================================================================================================
class PublisherNormaliser(object):
    """ Class to keep data and methods for publisher name normalisation """

    STR_PUBLISHER_NAME_MAP_FILE = Config.STR_DATA_DIRECTORY + 'publisher_name_map.tsv'

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self):
        """ Create name mapping dictionary for processing """
        self.dct_publisher_name_map = {}
        fp_publisher_map = open(self.STR_PUBLISHER_NAME_MAP_FILE, 'r')
        for str_row in fp_publisher_map:
            lst_row = str_row.split('\t')
            self.dct_publisher_name_map[lst_row[0].lower()] = lst_row[1].strip()
        fp_publisher_map.close()
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def normalise(self, str_publisher_name_in, str_doi):
        """ The main procedure to look up publisher name in name map and CrossRef. Calls sub-methods. """
        # Check if we already have this name in the map
        str_publisher_name_lower = str_publisher_name_in.strip().lower()
        if str_publisher_name_lower in self.dct_publisher_name_map.keys():
            str_publisher_name_normalised = self.dct_publisher_name_map[str_publisher_name_lower]
            if str_publisher_name_normalised != str_publisher_name_in:
                print 'NOTE: Name "{}" normalised to "{}"'.format(str_publisher_name_in, str_publisher_name_normalised)
            return str_publisher_name_normalised
        elif str_doi:
            # Look up in CrossRef - ToDo: Problem here if HTTP error instead of tuple returned
            dct_crossref_result = self.get_crossref_names(str_doi)
            if dct_crossref_result['error']:
                print('!ERROR: {}'.format(dct_crossref_result['error_reason']))
                print 'WARNING: No normalisation of name {}'.format(str_publisher_name_in)
                return str_publisher_name_in
            else:
                str_publisher_name_normalised = self.ask_user(str_publisher_name_in, dct_crossref_result)
            return str_publisher_name_normalised
        else:
            print 'WARNING: No normalisation of name {}'.format(str_publisher_name_in)
            return str_publisher_name_in
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def ask_user(self, str_publisher_name_in, dct_crossref_result):
        """ Ask opinion from user and return choice """
        print 'NOTE: Several name choices found. Please choose one alternative or enter new suggested name'
        print '1) {}'.format(str_publisher_name_in)
        print '2) {}'.format(dct_crossref_result['publisher'])
        print '3) {}'.format(dct_crossref_result['prefix'])
        print '4) Enter new preferred name'
        str_choice = raw_input('Choose [1] or enter new name:  ')
        if str_choice == '1':
            str_publisher_name_normalised = str_publisher_name_in.strip()
        elif str_choice == '2':
            str_publisher_name_normalised = dct_crossref_result['publisher'].strip()
        elif str_choice == '3':
            str_publisher_name_normalised = dct_crossref_result['prefix'].strip()
        elif str_choice:
            str_publisher_name_normalised = str_choice.strip()
        else:
            str_publisher_name_normalised = str_publisher_name_in
        # Add choice to mapping dictionary
        self.dct_publisher_name_map[str_publisher_name_in.lower()] = str_publisher_name_normalised
        return str_publisher_name_normalised
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def write_new_publisher_name_map(self):
        """ Write the new name map to file to remember for next processing """
        print('\nINFO: Updating publisher name normalisation file {}\n'.format(self.STR_PUBLISHER_NAME_MAP_FILE))
        fp_name_map_file = open(self.STR_PUBLISHER_NAME_MAP_FILE, 'w')
        for str_key in self.dct_publisher_name_map.keys():
            fp_name_map_file.write('{}\t{}\n'.format(str_key, self.dct_publisher_name_map[str_key]))
        fp_name_map_file.close()
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def get_crossref_names(self, doi):
        """ Get Crossref info
            <crm-item name="publisher-name" type="string">Institute of Electrical and Electronics Engineers (IEEE)</crm-item>
            <crm-item name="prefix-name" type="string">Institute of Electrical and Electronics Engineers</crm-item>
        """
        dct_crossref_lookup_result = dict(
            error = False,
            error_reason = '',
            publisher = '',
            prefix = '',
        )
        url = 'http://data.crossref.org/' + doi
        headers = {"Accept": "application/vnd.crossref.unixsd+xml"}
        req = urllib2.Request(url, None, headers)
        try:
            response = urllib2.urlopen(req)
            content_string = response.read()
            root = ET.fromstring(content_string)
            prefix_name_result = root.findall(".//cr_qr:crm-item[@name='prefix-name']",
                                  {"cr_qr": "http://www.crossref.org/qrschema/3.0"})
            publisher_name_result = root.findall(".//cr_qr:crm-item[@name='publisher-name']",
                                  {"cr_qr": "http://www.crossref.org/qrschema/3.0"})
            # return publisher_name_result[0].text, prefix_name_result[0].text
            dct_crossref_lookup_result['publisher'] = publisher_name_result[0].text
            dct_crossref_lookup_result['prefix'] = prefix_name_result[0].text
        except urllib2.HTTPError as httpe:
            dct_crossref_lookup_result['error'] = True
            code = str(httpe.getcode())
            dct_crossref_lookup_result['error_reason'] = "HTTPError: {} - {}".format(code, httpe.reason)
        except urllib2.URLError as urle:
            dct_crossref_lookup_result['error'] = True
            dct_crossref_lookup_result['error_reason'] = "URLError: {}".format(urle.reason)
        except ET.ParseError as etpe:
            dct_crossref_lookup_result['error'] = True
            dct_crossref_lookup_result['error_reason'] = "ElementTree ParseError: {}".format(str(etpe))
        return dct_crossref_lookup_result
    # ------------------------------------------------------------------------------------------------------------------

# ======================================================================================================================


# ======================================================================================================================
class CSVColumn(object):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    NONE = "non-required"

    OW_ALWAYS = 0
    OW_ASK = 1
    OW_NEVER = 2

    _OW_MSG = (u"\033[91mConflict\033[0m: Existing non-NA value " +
               u"\033[93m{ov}\033[0m in column \033[93m{name}\033[0m is to be " +
               u"replaced by new value \033[93m{nv}\033[0m.\nAllow overwrite?\n" +
               u"1) Yes\n2) Yes, and always replace \033[93m{ov}\033[0m by " +
               "\033[93m{nv}\033[0m in this column\n3) Yes, and always " +
               "overwrite in this column\n4) No\n5) No, and never replace " +
               "\033[93m{ov}\033[0m by \033[93m{nv}\033[0m in this " +
               "column\n6) No, and never overwrite in this column\n>")

    # ------------------------------------------------------------------------------------------------------------------
    def __init__(self, column_type, requirement, index=None, column_name="", overwrite=OW_ASK):
        self.column_type = column_type
        self.requirement = requirement
        self.index = index
        self.column_name = column_name
        self.overwrite = overwrite
        self.overwrite_whitelist = {}
        self.overwrite_blacklist = {}
    # ------------------------------------------------------------------------------------------------------------------

    # ------------------------------------------------------------------------------------------------------------------
    def check_overwrite(self, old_value, new_value):
        if old_value == new_value:
            return old_value
        # Priority: Empty or NA values will always be overwritten.
        if old_value == "NA":
            return new_value
        if old_value.strip() == "":
            return new_value
        if self.overwrite == CSVColumn.OW_ALWAYS:
            return new_value
        if self.overwrite == CSVColumn.OW_NEVER:
            return old_value
        if old_value in self.overwrite_blacklist:
            if self.overwrite_blacklist[old_value] == new_value:
                return old_value
        if old_value in self.overwrite_whitelist:
            return new_value
        msg = CSVColumn._OW_MSG.format(ov=old_value, name=self.column_name,
                                       nv=new_value)
        msg = msg.encode("utf-8")
        ret = raw_input(msg)
        while ret not in ["1", "2", "3", "4", "5", "6"]:
            ret = raw_input("Please select a number between 1 and 5:")
        if ret == "1":
            return new_value
        if ret == "2":
            self.overwrite_whitelist[old_value] = new_value
            return new_value
        if ret == "3":
            self.overwrite = CSVColumn.OW_ALWAYS
            return new_value
        if ret == "4":
            return old_value
        if ret == "5":
            self.overwrite_blacklist[old_value] = new_value
            return old_value
        if ret == "6":
            self.overwrite = CSVColumn.OW_NEVER
            return old_value
    # ------------------------------------------------------------------------------------------------------------------

# ======================================================================================================================

# Invoke the main loop
# ======================================================================================================================
if __name__ == '__main__':
    main()
# ======================================================================================================================
