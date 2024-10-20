import os
import sys
import argparse
import datetime
import sqlite3
import traceback
import logging
import xxhash
from pathlib import Path, PurePath
from queue import Queue
from threading import Thread, Lock
from tqdm import tqdm

# Global list for processed data; shared between threads
processed_data = []

import signal

def signal_handler(sig, frame):
    print("\nInterrupt received. Saving progress and exiting...")
    if processed_data:
        insert_data_batch(processed_data)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# Database Functions
def create_db_and_table():
    """
    Create the SQLite database and the files table if they don't exist.
    Also creates an index on the hash column for faster queries.
    """
    DB_NAME = os.environ.get('DB_NAME', 'file_data.db')

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT,
            path TEXT UNIQUE,
            size INTEGER,
            last_modified DATETIME,
            last_checked DATETIME
        )
        ''')
        # Create index on hash
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON files (hash)')
        conn.commit()

def get_db_connection():
    """
    Get a connection to the SQLite database.
    Also ensures that the index on the hash column exists.
    
    Returns:
        sqlite3.Connection: An open connection to the database.
    """
    DB_NAME = os.environ.get('DB_NAME', 'file_data.db')

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Create index on hash if it doesn't exist
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON files (hash);')
    conn.commit()
    return conn

def close_db_connection(conn):
    """
    Close the given database connection if it's open.
    
    Args:
        conn (sqlite3.Connection): The database connection to close.
    """
    if conn:
        conn.close()

# File Processing Functions
def process_file(file_path):
    """
    Process a single file: calculate its hash and collect metadata.
    
    Args:
        file_path (str or Path): The path to the file to process.
    
    Returns:
        tuple: A tuple containing (file_hash, file_path, size, last_modified), or None if an error occurred.
    """
    # Ensure file_path is a Path object and get the absolute path
    if not isinstance(file_path, Path):
        file_path = Path(file_path)
    file_path = file_path.absolute()

    # Check if the file exists
    if not file_path.exists():
        return None

    print(f"PyDupes: Processing {file_path}")
    try:
        # Get file size and last modified time
        stat = file_path.stat()
        size = stat.st_size
        last_modified = datetime.datetime.fromtimestamp(stat.st_mtime)

        # Calculate xxHash
        hasher = xxhash.xxh64()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                hasher.update(chunk)

        file_hash = hasher.hexdigest()
        return file_hash, str(file_path), size, last_modified
    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}")
        traceback.print_exc()
        return None  # Return None if there was an error

def worker_thread(file_queue, worker_pbar, overall_pbar, lock, thread_id):
    """
    Worker thread function for processing files.
    Each thread processes files from the file_queue and updates progress bars.

    Args:
        file_queue (Queue): A queue containing file paths to process.
        worker_pbar (tqdm): A progress bar for this worker thread.
        overall_pbar (tqdm): The overall progress bar.
        lock (Lock): A threading lock for synchronizing access to shared resources.
        thread_id (int): The ID of the thread.
    """
    while not file_queue.empty():
        try:
            file_path = file_queue.get_nowait()
        except Exception:
            break  # Queue is empty

        file_path = Path(file_path).resolve()
        if not file_path.exists():
            with lock:
                logging.warning(f"File does not exist: {file_path}")
                overall_pbar.update(1)
            continue

        try:
            # Get file size and last modified time
            stat = file_path.stat()
            size = stat.st_size
            last_modified = datetime.datetime.fromtimestamp(stat.st_mtime)

            # Reset the worker progress bar for the new file
            with lock:
                worker_pbar.reset(total=size)
                worker_pbar.set_description(f"Thread {thread_id+1}: {file_path.name[:30]}")  # Truncate if necessary

            # Calculate xxHash and update progress
            hasher = xxhash.xxh64()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    with lock:
                        worker_pbar.update(len(chunk))

            file_hash = hasher.hexdigest()

            # Store the result in the shared list
            with lock:
                processed_data.append((file_hash, str(file_path), size, last_modified))
                overall_pbar.update(1)
                logging.info(f"Processed file: {file_path}")

        except Exception as e:
            with lock:
                logging.error(f"Error processing {file_path}: {e}")
                traceback.print_exc()
                overall_pbar.update(1)
    # Close the worker progress bar when done
    with lock:
        worker_pbar.close()

def insert_data(data):
    """
    Insert or update a single file record in the database.

    Args:
        data (tuple): A tuple containing (file_hash, file_path, size, last_modified).
    """
    now = datetime.datetime.now()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT id FROM files WHERE path = ?
        ''', (data[1],))
        existing_file = cursor.fetchone()

        if existing_file:
            cursor.execute('''
            UPDATE files
            SET hash = ?, size = ?, last_modified = ?, last_checked = ?
            WHERE id = ?
            ''', (data[0], data[2], data[3], now, existing_file[0]))
            conn.commit()

            print(f"PyDupes: Updated {data[1]}")
            return

        cursor.execute('''
        INSERT INTO files (hash, path, size, last_modified, last_checked)
        VALUES (?, ?, ?, ?, ?)
        ''', (*data, now))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error inserting data: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        close_db_connection(conn)

def insert_data_batch(data_list):
    """
    Perform a bulk insert or update of file records in the database.

    Args:
        data_list (list): A list of tuples, each containing (file_hash, file_path, size, last_modified).
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = '''
        INSERT OR REPLACE INTO files (hash, path, size, last_modified, last_checked)
        VALUES (?, ?, ?, ?, ?)
        '''
        now = datetime.datetime.now()
        data_with_timestamp = [(*data, now) for data in data_list]
        cursor.executemany(sql, data_with_timestamp)
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error during batch insert: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error during batch insert: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        close_db_connection(conn)

def walk_directory(directory):
    """
    Generator function to walk through a directory and yield file paths.

    Args:
        directory (str): The directory to scan.

    Yields:
        str: The full path to each file found.
    """
    for root, dirs, files in os.walk(directory, topdown=True, onerror=None, followlinks=False):
        for name in files:
            try:
                yield os.path.join(root, name)
            except Exception as e:
                print(f"Error accessing file {name} in {root}: {str(e)}", file=sys.stderr)

        # Handle permission errors for directories
        dirs[:] = [d for d in dirs if os.access(os.path.join(root, d), os.R_OK)]

def load_existing_paths():
    """
    Load existing file paths from the database into a set for quick lookup.

    Returns:
        set: A set of file paths currently stored in the database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT path FROM files')
    rows = cursor.fetchall()
    close_db_connection(conn)
    existing_paths = set(row[0] for row in rows)
    return existing_paths

# Duplicate Handling Functions
def rescan_duplicates():
    """
    Rescan duplicate files to update their hashes and metadata in the database.

    Returns:
        list: A list of tuples containing (hash, path) of duplicate files.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    SELECT hash, path FROM files
    WHERE hash IN (
        SELECT hash FROM files
        GROUP BY hash
        HAVING COUNT(*) > 1
    )
    ORDER BY hash
    ''')
    duplicates = cursor.fetchall()
    close_db_connection(conn)

    # Rescan each duplicate file
    for duplicate in duplicates:
        data = process_file(duplicate[1])
        if data is not None:
            insert_data(data)

    return duplicates

def get_duplicates(preferred_source_directories=None, within_directory=None):
    """
    Retrieve a list of duplicate files, optionally filtered by directory preferences and location.

    Args:
        preferred_source_directories (list): A list of directories that have higher preference for original files.
        within_directory (str): Only examine duplicates within this directory.

    Returns:
        list: A list of dictionaries, each representing a group of duplicates.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all files (or files within the specified directory)
    if within_directory:
        within_directory = os.path.normpath(os.path.abspath(within_directory))
        cursor.execute('''
        SELECT hash, path FROM files WHERE path LIKE ?
        AND hash in 
            (SELECT hash from files
             WHERE path LIKE ?
             GROUP BY hash HAVING COUNT(*) > 1)
        ''', (f'{within_directory}%',f'{within_directory}%'))
    else:
        # Get all files
        cursor.execute('''
        SELECT hash, path FROM files
        WHERE hash in 
            (SELECT hash from files
             GROUP BY hash HAVING COUNT(*) > 1)
        ''')
    all_files = cursor.fetchall()

    # Organize files by hash
    files_by_hash = {}
    for file_hash, file_path in all_files:
        files_by_hash.setdefault(file_hash, []).append(file_path)

    unique_hashes = set(files_by_hash.keys())
    duplicates_list = []

    for file_hash, paths in files_by_hash.items():
        if len(paths) < 2:
            continue  # Not enough files for duplicates

    # Process each group of duplicates
    for file_hash in unique_hashes:
        # Get the list of paths for this hash
        files = files_by_hash[file_hash]

        # Select the original file
        original, duplicates = select_original(files, preferred_source_directories=preferred_source_directories)

        # Add the duplicates to the list
        duplicates_list.append({
            'hash': file_hash,
            'original': original,
            'duplicates': duplicates,
            'no_matching_original': False
        })

    close_db_connection(conn)
    return duplicates_list
from pathlib import PurePath

def select_original(files, preferred_source_directories=None):
    """
    Select the original file from a list of duplicate files.
    The original is decided based on whether a preferred directory is specified, and found, then
    by the fewest folders, shortest path, and alphabetical order.

    :param files: List of files to check
    :param preferred_source_directories: List of directories to prefer when selecting the original
    :return: Tuple containing the original file and the list of remaining files
    """

    ###### Preferred directory logic ######
    preferred_directory_files = []
    if preferred_source_directories:
        for directory in preferred_source_directories:
            print(f"Checking for original in preferred directory: {directory}")
            for file in files:
                print(file)
                if file.startswith(directory):
                    print(f"Found match in {file} for directory {directory}")
                    preferred_directory_files.append(file)

            # We found one or more files in the preferred directory, so we can break the loop
            if preferred_directory_files:
                break

    # If we found one and only one, it must be the original
    if len(preferred_directory_files) == 1:
        original = preferred_directory_files[0]
        files.remove(original)
        return original, files

    # If no preferred directory files are found, use all files
    if not preferred_directory_files:
        preferred_directory_files = files.copy()

    ###### Fewest folders logic ######
    # If we found more than one file in the preferred directory,
    # we need to select the original from them
    # Add the number of folders to each file info
    files_by_folders = {}
    for file in preferred_directory_files:
        path_obj = PurePath(file)
        num_folders = len(path_obj.parts) - 1  # Subtract 1 to exclude the file name
        files_by_folders.setdefault(num_folders, []).append(file)

    # Find the fewest number of folders
    fewest_folders = min(files_by_folders.keys())
    fewest_folder_files = files_by_folders[fewest_folders]

    # If there is only one file with the fewest number of folders
    if len(fewest_folder_files) == 1:
        original = fewest_folder_files[0]
        files.remove(original)
        return original, files

    ###### Shortest path logic ######
    # From the files with the fewest folders, find those with the shortest path length
    files_by_path_length = {}
    for file in fewest_folder_files:
        path_length = len(file)
        files_by_path_length.setdefault(path_length, []).append(file)

    # Find the minimum path length
    min_path_length = min(files_by_path_length.keys())
    min_path_length_files = files_by_path_length[min_path_length]

    # If there is only one file with the shortest path length
    if len(min_path_length_files) == 1:
        original = min_path_length_files[0]
        files.remove(original)
        return original, files

    ###### Alphabetical logic ######
    # Sort the files alphabetically
    alphabetical_files = sorted(min_path_length_files)

    # Select the first file alphabetically
    original = alphabetical_files[0]
    files.remove(original)
    return original, files


def list_duplicates_excluding_original(output_file=None, preferred_source_directories=None, within_directory=None):
    """
    List duplicates excluding the original file.

    Args:
        output_file (str): Path to the output file where the list will be written. If None, prints to console.
        preferred_source_directories (list): List of directories with preference for selecting originals.
        within_directory (str): Only examine duplicates within this directory. I.e., only look for files that are
        duplicated within this directory.

    Returns:
        list: A list of duplicate file paths excluding the original files.
    """
    duplicates_list = get_duplicates(preferred_source_directories=preferred_source_directories, within_directory=within_directory)
    duplicates_excl_original = []

    for group in duplicates_list:
        original_file = group['original']
        duplicates = group['duplicates']

        if group['no_matching_original']:
            print(f"Duplicate group with hash {group['hash']} has no matching original in specified directories.")
        else:
            print(f"Original file for duplicated hash {group['hash']}: {original_file}")

        duplicates_excl_original.extend(duplicates)

    # Output the list of duplicates excluding originals
    if output_file:
        try:
            with open(output_file, 'w') as f:
                for dup_file in duplicates_excl_original:
                    f.write(f"{dup_file}\n")
            print(f"\nList of duplicate files excluding originals has been written to {output_file}")
        except Exception as e:
            print(f"Error writing to file {output_file}: {e}", file=sys.stderr)
    else:
        print("\nList of duplicate files excluding originals:")
        for dup_file in duplicates_excl_original:
            print(dup_file)

    return duplicates_excl_original

def list_duplicates_csv(output_file, preferred_source_directories=None, within_directory=None):
    """
    List duplicates and originals in CSV format.

    Args:
        output_file (str): Path to the output CSV file.
        preferred_source_directories (list): List of directories with preference for selecting originals.
        within_directory (str): Only examine duplicates within this directory.

    Returns:
        list: A list of dictionaries containing duplicates and original file information.
    """
    import csv

    duplicates_list = get_duplicates(preferred_source_directories=preferred_source_directories, within_directory=within_directory)
    duplicates_info = []

    for group in duplicates_list:
        original = group['original']
        duplicates = group['duplicates']

        if group['no_matching_original']:
            status_flag = 'duplicate - no matching original'
            print(f"Duplicate group with hash {group['hash']} has no matching original in specified directories.")
        else:
            status_flag = 'original'
            print(f"Original file for hash {group['hash']}: {original}")

        # Add original file info
        duplicates_info.append({
            'status': status_flag,
            'path': original,
            'hash': group['hash']
        })

        # Add duplicates info
        for duplicate in duplicates:
            duplicates_info.append({
                'status': 'duplicate',
                'path': duplicate,
                'hash': group['hash']
            })

    # Output the data to a CSV file
    try:
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['status', 'path', 'hash']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            for info in duplicates_info:
                writer.writerow(info)
        print(f"\nList of duplicates and originals has been written to {output_file}")
    except Exception as e:
        print(f"Error writing to file {output_file}: {e}", file=sys.stderr)

    return duplicates_info

def delete_duplicates(preferred_source_directories=None, output_file=None,
                      overwrite=False, append=False, simulate_delete=False, within_directory=None):
    """
    Delete duplicate files, optionally logging actions to a CSV file.

    Args:
        preferred_source_directories (list): List of directories with preference for selecting originals.
        output_file (str): Path to the output CSV log file.
        overwrite (bool): Whether to overwrite the output file if it exists.
        append (bool): Whether to append to the output file if it exists.
        simulate_delete (bool): If True, do not actually delete files.
        within_directory (str): Only delete duplicates within this directory.
    """
    duplicates_list = get_duplicates(preferred_source_directories=preferred_source_directories, within_directory=within_directory)
    total_deleted = 0
    deleted_files = []

    writer = None
    csvfile = None

    # Handle output file options
    if output_file:
        file_exists = os.path.isfile(output_file)
        file_mode = 'w'

        if file_exists:
            if overwrite:
                file_mode = 'w'
            elif append:
                file_mode = 'a'
            else:
                print(f"Error: Output file '{output_file}' already exists. Use --overwrite or --append to specify the desired behavior.", file=sys.stderr)
                return

        try:
            csvfile = open(output_file, file_mode, newline='', encoding='utf-8')
            fieldnames = ['status', 'path', 'hash']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if file_mode == 'w' or (file_mode == 'a' and os.stat(output_file).st_size == 0):
                writer.writeheader()
                csvfile.flush()
        except Exception as e:
            print(f"Error opening file {output_file}: {e}", file=sys.stderr)
            writer = None
            csvfile = None

    try:
        for group in duplicates_list:
            original_path = group['original']

            if group['no_matching_original']:
                status_flag = 'kept - no matching original'
                print(f"Duplicate group with hash {group['hash']} has no matching original in specified directories.")
            else:
                status_flag = 'kept'
                print(f"Original file for hash {group['hash']}: {original_path}")

            # Log the original file
            log_entry = {
                'status': status_flag,
                'path': original_path,
                'hash': group['hash']
            }
            if writer:
                writer.writerow(log_entry)
                csvfile.flush()

            for dup_file in group['duplicates']:

                try:
                    if not simulate_delete:
                        os.remove(dup_file)
                        print(f"Deleted duplicate file: {dup_file}")
                        status = 'deleted'
                        total_deleted += 1
                        deleted_files.append(dup_file)
                    else:
                        print(f"Simulated deletion of duplicate file: {dup_file}")
                        status = 'deleted (simulated)'
                        deleted_files.append(dup_file)
                        total_deleted += 1

                except Exception as e:
                    print(f"Error deleting file {dup_file}: {e}", file=sys.stderr)
                    status = f'error - {e}'

                # Log the duplicate file
                log_entry = {
                    'status': status,
                    'path': dup_file,
                    'hash': group['hash']
                }
                if writer:
                    writer.writerow(log_entry)
                    csvfile.flush()
    finally:
        # Ensure the CSV file is properly closed
        if csvfile:
            csvfile.close()

    print(f"\nTotal duplicates deleted: {total_deleted}")

    if simulate_delete:
        print("Note: Deletion was simulated. No files were actually deleted.")

    return deleted_files

def remove_missing_files():
    """
    Remove entries from the database for files that no longer exist on disk.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT path FROM files')
    rows = cursor.fetchall()
    total_removed = 0

    paths_to_remove = []

    for row in rows:
        file_path = row[0]
        if not os.path.exists(file_path):
            print(f"Marking {file_path} for removal from database as it no longer exists on disk.")
            paths_to_remove.append((file_path,))

    if paths_to_remove:
        cursor.executemany('DELETE FROM files WHERE path = ?', paths_to_remove)
        conn.commit()
        total_removed = len(paths_to_remove)
    else:
        print("No missing files found in the database.")

    close_db_connection(conn)
    print(f"Total entries removed from database: {total_removed}")

def process_batch(files, num_threads, overall_pbar):
    """
    Process a batch of files using X threads.

    Args:
        files (list): List of file paths to process.
        num_threads (int): Number of threads to use.
        overall_pbar (tqdm): Overall progress bar.

    Returns:
        list: Processed data from the batch.
    """
    from threading import Thread

    batch_processed_data = []
    threads = []
    lock = Lock()

    # Define worker function
    def worker(file_path):
        result = process_file(file_path)
        if result:
            with lock:
                batch_processed_data.append(result)
        with lock:
            overall_pbar.update(1)

    # Start threads for the batch
    for file_path in files:
        t = Thread(target=worker, args=(file_path,))
        t.start()
        threads.append(t)

    # Wait for all threads in the batch to finish
    for t in threads:
        t.join()

    return batch_processed_data


# Main Function
def main(directory, skip_existing=False, num_threads=None):
    print("Initializing database and tables...")
    create_db_and_table()

    print(f"Scanning directory: {directory}")
    # Get all files in the specified directory and subdirectories
    files = list(walk_directory(directory))
    print(f"Total files found: {len(files)}")

    # Exclude existing files if skip_existing is True
    if skip_existing:
        print("Loading existing file paths from database to skip already processed files...")
        existing_paths = load_existing_paths()
        files_to_process = [file for file in files if str(Path(file).resolve()) not in existing_paths]
        print(f"Files to process after skipping existing: {len(files_to_process)}")
    else:
        files_to_process = files

    if not files_to_process:
        print("No new files to process.")
        return

    print(f"Processing files with {num_threads} threads in batches of {num_threads} files...")
    total_files = len(files_to_process)
    batches = [files_to_process[i:i + num_threads] for i in range(0, total_files, num_threads)]

    # Initialize the overall progress bar
    overall_pbar = tqdm(total=total_files, desc="Total Progress", unit='file', leave=True)

    for batch_num, batch_files in enumerate(batches, start=1):
        print(f"\nProcessing batch {batch_num}/{len(batches)}...")
        batch_processed_data = []

        threads = []
        for file_path in batch_files:
            t = Thread(target=process_file_wrapper, args=(file_path, batch_processed_data))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()
            overall_pbar.update(1)

        # Insert data into the database after each batch
        if batch_processed_data:
            insert_data_batch(batch_processed_data)

    overall_pbar.close()
    print("\nProcessing complete.")

def process_file_wrapper(file_path, batch_processed_data):
    result = process_file(file_path)
    if result:
        batch_processed_data.append(result)


# Entry Point
if __name__ == "__main__":
    # Argument parser and command handling
    parser = argparse.ArgumentParser(description='Process files and find duplicates.')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Subparser for the 'process' command
    parser_process = subparsers.add_parser('process', help='Process a directory to find duplicates')
    parser_process.add_argument('directory', help='Directory to process')
    parser_process.add_argument('--skip-existing', action='store_true',
                                help='Skip processing files that are already in the database')
    default_threads = os.cpu_count() or 1
    parser_process.add_argument('--threads', type=int, default=default_threads,
                                help='Number of threads for hashing (default: number of CPU cores)')
    parser_process.add_argument('--log-file', help='Path to log file for detailed output')

    # Subparser for the 'rescan-duplicates' command
    parser_rescan = subparsers.add_parser('rescan-duplicates', help='Rescan duplicate files')

    parser_clean_db = subparsers.add_parser('clean-db', help='Remove entries from the database for files that no longer exist on disk')

    # Subparser for the 'list-duplicates' command
    parser_list = subparsers.add_parser('list-duplicates', help='List duplicates excluding originals')
    parser_list.add_argument('-o', '--output', help='Output file to write the list to')
    parser_list.add_argument('--prefer-directory',
                             help='Preferred source directories for selecting original files (comma-separated if multiple)')
    parser_list.add_argument('--within-directory', help='Only examine duplicates within this directory')

    # Subparser for the 'list-duplicates-csv' command
    parser_csv = subparsers.add_parser('list-duplicates-csv', help='List duplicates and originals in CSV format')
    parser_csv.add_argument('-o', '--output', required=True, help='Output CSV file to write the list to')
    parser_csv.add_argument('--prefer-directory',
                            help='Preferred source directories for selecting original files (comma-separated if multiple)')
    parser_csv.add_argument('--within-directory', help='Only examine duplicates within this directory')

    # Subparser for the 'delete-duplicates' command
    parser_delete = subparsers.add_parser('delete-duplicates', help='Delete duplicate files')
    parser_delete.add_argument('--prefer-directory',
                               help='Preferred source directories for selecting original files (comma-separated if multiple)')
    parser_delete.add_argument('-o', '--output', help='Output CSV file to log the deleted files')
    group = parser_delete.add_mutually_exclusive_group()
    group.add_argument('--overwrite', action='store_true', help='Overwrite the output file if it exists')
    group.add_argument('--append', action='store_true', help='Append to the output file if it exists')
    parser_delete.add_argument('--simulate-delete', action='store_true',
                               help='Simulate deletion without actually deleting files')
    parser_delete.add_argument('--within-directory', help='Only examine duplicates within this directory')

    args = parser.parse_args()

    # Set up logging
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    if getattr(args, 'log_file', None):
        logging.basicConfig(filename=args.log_file, level=logging.INFO, format=log_format)
    else:
        # Set logging level to WARNING to suppress INFO messages in console
        logging.basicConfig(level=logging.WARNING, format=log_format)

    if args.command == 'process':
        directory_to_process = args.directory
        if not os.path.isdir(directory_to_process):
            print(f"Error: {directory_to_process} is not a valid directory", file=sys.stderr)
            sys.exit(1)
        skip_existing = args.skip_existing
        num_threads = args.threads
        main(directory_to_process, skip_existing=skip_existing, num_threads=num_threads)

    elif args.command == 'rescan-duplicates':
        rescan_duplicates()

    elif args.command == 'clean-db':
        remove_missing_files()

    elif args.command == 'list-duplicates':
        # Handle arguments specific to this command
        if args.prefer_directory:
            preferred_directories = [d.strip() for d in args.prefer_directory.split(',')]
        else:
            preferred_directories = None

        within_directory = args.within_directory

        list_duplicates_excluding_original(
            output_file=args.output,
            preferred_source_directories=preferred_directories,
            within_directory=within_directory
        )

    elif args.command == 'list-duplicates-csv':
        # Handle arguments specific to this command
        if args.prefer_directory:
            preferred_directories = [d.strip() for d in args.prefer_directory.split(',')]
        else:
            preferred_directories = None

        within_directory = args.within_directory

        list_duplicates_csv(
            output_file=args.output,
            preferred_source_directories=preferred_directories,
            within_directory=within_directory
        )

    elif args.command == 'delete-duplicates':
        # Handle arguments specific to this command
        if args.prefer_directory:
            preferred_directories = [d.strip() for d in args.prefer_directory.split(',')]
        else:
            preferred_directories = None

        within_directory = args.within_directory

        delete_duplicates(
            preferred_source_directories=preferred_directories,
            output_file=args.output,
            overwrite=args.overwrite,
            append=args.append,
            simulate_delete=args.simulate_delete,
            within_directory=within_directory
        )

    else:
        parser.print_help()