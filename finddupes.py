import os
import sys
import argparse
import datetime
import hashlib
import sqlite3
import traceback
import csv
from pathlib import Path, PurePath

DB_NAME = 'file_data.db'

def create_db_and_table():
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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Create index on hash if it doesn't exist
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_hash ON files (hash);')
    conn.commit()
    return conn

def close_db_connection(conn):
    if conn:
        conn.close()

def process_file(file_path):
    if not os.path.exists(file_path):
        print(f"PyDupes: {file_path} no longer exists, removing from database")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
        DELETE FROM files WHERE path = ?
        ''', (file_path,))
        conn.commit()
        close_db_connection(conn)
        return None

    file_path = Path(file_path).resolve()  # Get the full path
    print(f"PyDupes: Processing {file_path}")

    try:
        # Get file size and last modified time
        stat = file_path.stat()
        size = stat.st_size
        last_modified = datetime.datetime.fromtimestamp(stat.st_mtime)

        # Calculate MD5 hash
        with open(file_path, "rb") as f:
            file_hash = hashlib.md5()
            chunk = f.read(8192)
            while chunk:
                file_hash.update(chunk)
                chunk = f.read(8192)

        return file_hash.hexdigest(), str(file_path), size, last_modified
    except Exception as e:
        print(f"Error processing {file_path}: {str(e)}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return None  # Return None if there was an error

def insert_data(data):
    now = datetime.datetime.now()
    conn = get_db_connection()
    try:
        # If this file already exists in the database, update the last_checked time
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

            print (f"PyDupes: Updated {data[1]}")
            return

        with conn:  # This automatically handles commit/rollback
            cursor = conn.cursor()
            cursor.execute('''
            INSERT OR REPLACE INTO files (hash, path, size, last_modified, last_checked)
            VALUES (?, ?, ?, ?, ?)
            ''', (*data, now))
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error inserting data: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        close_db_connection(conn)

def walk_directory(directory):
    for root, dirs, files in os.walk(directory, topdown=True, onerror=None, followlinks=False):
        for name in files:
            try:
                yield os.path.join(root, name)
            except Exception as e:
                print(f"Error accessing file {name} in {root}: {str(e)}", file=sys.stderr)

        # Handle permission errors for directories
        dirs[:] = [d for d in dirs if os.access(os.path.join(root, d), os.R_OK)]

# Rescan duplicates
def rescan_duplicates():
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

    # Rescan each duplicate file, no thread pool
    for duplicate in duplicates:
        data = process_file(duplicate[1])
        if data is not None:
            insert_data(data)

    return duplicates

def get_duplicates(preferred_source_directory=None):
    from pathlib import PurePath
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all hashes where there are duplicates
    cursor.execute('''
    SELECT hash FROM files
    GROUP BY hash
    HAVING COUNT(*) > 1
    ''')
    hashes = [row[0] for row in cursor.fetchall()]

    duplicates_list = []

    for file_hash in hashes:
        cursor.execute('''
        SELECT path FROM files WHERE hash = ?
        ''', (file_hash,))
        paths = [row[0] for row in cursor.fetchall()]

        # Prepare a list to hold file info
        file_info = []
        for file_path in paths:
            # Create a PurePath object
            path_obj = PurePath(file_path)
            # Get the parts of the path
            path_parts = path_obj.parts
            # Number of folders is total parts minus 1 (for the file name)
            num_folders = len(path_parts) - 1
            # Length of the entire path string
            path_length = len(str(path_obj))
            # Determine if the path matches the preferred source directory
            if preferred_source_directory:
                # Use PurePath for comparison
                preferred_path = PurePath(preferred_source_directory)
                # Check if file_path is under preferred_source_directory
                is_in_preferred = preferred_path in path_obj.parents or preferred_path == path_obj.parent
            else:
                is_in_preferred = False
            file_info.append({
                'path': file_path,
                'num_folders': num_folders,
                'path_length': path_length,
                'hash': file_hash,
                'is_in_preferred': is_in_preferred
            })

        original_file_info = None
        no_matching_original = False

        if preferred_source_directory:
            # Filter files that are under the preferred source directory
            preferred_files = [info for info in file_info if info['is_in_preferred']]
            if preferred_files:
                # From preferred files, select the one with least number of folders, then shortest path length
                min_num_folders = min(info['num_folders'] for info in preferred_files)
                candidates = [info for info in preferred_files if info['num_folders'] == min_num_folders]
                min_path_length = min(info['path_length'] for info in candidates)
                original_candidates = [info for info in candidates if info['path_length'] == min_path_length]
                original_file_info = original_candidates[0]
            else:
                # No files match the preferred source directory
                no_matching_original = True
                # Proceed with the default method
                min_num_folders = min(info['num_folders'] for info in file_info)
                candidates = [info for info in file_info if info['num_folders'] == min_num_folders]
                min_path_length = min(info['path_length'] for info in candidates)
                original_candidates = [info for info in candidates if info['path_length'] == min_path_length]
                original_file_info = original_candidates[0]
        else:
            # Proceed with the default method
            min_num_folders = min(info['num_folders'] for info in file_info)
            candidates = [info for info in file_info if info['num_folders'] == min_num_folders]
            min_path_length = min(info['path_length'] for info in candidates)
            original_candidates = [info for info in candidates if info['path_length'] == min_path_length]
            original_file_info = original_candidates[0]

        # Collect the duplicates excluding the original
        duplicates = [info for info in file_info if info['path'] != original_file_info['path']]

        duplicates_list.append({
            'hash': file_hash,
            'original': original_file_info,
            'duplicates': duplicates,
            'no_matching_original': no_matching_original
        })

    close_db_connection(conn)
    return duplicates_list

def list_duplicates_excluding_original(output_file=None, preferred_source_directory=None):
    duplicates_list = get_duplicates(preferred_source_directory=preferred_source_directory)
    duplicates_excl_original = []

    for group in duplicates_list:
        original_file = group['original']['path']
        duplicates = [info['path'] for info in group['duplicates']]

        if group['no_matching_original']:
            print(f"Duplicate group with hash {group['hash']} has no matching original in specified directory.")
        else:
            print(f"Original file for hash {group['hash']}: {original_file}")

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

def list_duplicates_csv(output_file, preferred_source_directory=None):
    import csv
    duplicates_list = get_duplicates(preferred_source_directory=preferred_source_directory)
    duplicates_info = []

    for group in duplicates_list:
        original_file_info = group['original']
        duplicates = group['duplicates']

        if group['no_matching_original']:
            status_flag = 'duplicate - no matching original path'
            print(f"Duplicate group with hash {group['hash']} has no matching original in specified directory.")
        else:
            status_flag = 'original'
            print(f"Original file for hash {group['hash']}: {original_file_info['path']}")

        # Add original file info
        duplicates_info.append({
            'status': status_flag,
            'path': original_file_info['path'],
            'hash': group['hash']
        })

        # Add duplicates info
        for info in duplicates:
            duplicates_info.append({
                'status': 'duplicate',
                'path': info['path'],
                'hash': group['hash']
            })

    # Output the data to a CSV file
    if output_file:
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
    else:
        # If no output file specified, print to console
        print("\nList of duplicates and originals:")
        for info in duplicates_info:
            print(f"{info['status']}, {info['path']}, {info['hash']}")

    return duplicates_info

def delete_duplicates(preferred_source_directory=None):
    import os
    duplicates_list = get_duplicates(preferred_source_directory=preferred_source_directory)
    files_deleted = []

    for group in duplicates_list:
        # Do not delete if there is no matching original
        if group['no_matching_original']:
            print(f"Skipping deletion for duplicates with hash {group['hash']} as no matching original path was found.")
            continue

        for dup_info in group['duplicates']:
            dup_file = dup_info['path']
            try:
                os.remove(dup_file)
                print(f"Deleted duplicate file: {dup_file}")
                files_deleted.append(dup_file)
            except Exception as e:
                print(f"Error deleting file {dup_file}: {e}", file=sys.stderr)

    print(f"\nTotal duplicates deleted: {len(files_deleted)}")
    return files_deleted

def main(directory):
    # Create database and table if they don't exist
    create_db_and_table()

    # Get all files in the specified directory and subdirectories
    files = walk_directory(directory)

    for file in files:
        data = process_file(file)
        if data is not None:
            insert_data(data)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process files and find duplicates.')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Subparser for the 'process' command
    parser_process = subparsers.add_parser('process', help='Process a directory to find duplicates')
    parser_process.add_argument('directory', help='Directory to process')

    # Subparser for the 'rescan-duplicates' command
    parser_rescan = subparsers.add_parser('rescan-duplicates', help='Rescan duplicate files')

    # Subparser for the 'list-duplicates' command
    parser_list = subparsers.add_parser('list-duplicates', help='List duplicates excluding originals')
    parser_list.add_argument('-o', '--output', help='Output file to write the list to')
    parser_list.add_argument('--prefer-directory', help='Preferred source directory for selecting original files')

    # Subparser for the 'list-duplicates-csv' command
    parser_csv = subparsers.add_parser('list-duplicates-csv', help='List duplicates and originals in CSV format')
    parser_csv.add_argument('-o', '--output', required=True, help='Output CSV file to write the list to')
    parser_csv.add_argument('--prefer-directory', help='Preferred source directory for selecting original files')

    # Subparser for the 'delete-duplicates' command
    parser_delete = subparsers.add_parser('delete-duplicates', help='Delete duplicate files')
    parser_delete.add_argument('--prefer-directory', help='Preferred source directory for selecting original files')

    args = parser.parse_args()

    if args.command == 'process':
        directory_to_process = args.directory
        if not os.path.isdir(directory_to_process):
            print(f"Error: {directory_to_process} is not a valid directory", file=sys.stderr)
            sys.exit(1)
        main(directory_to_process)
    elif args.command == 'rescan-duplicates':
        rescan_duplicates()
    elif args.command == 'list-duplicates':
        list_duplicates_excluding_original(output_file=args.output, preferred_source_directory=args.prefer_directory)
    elif args.command == 'list-duplicates-csv':
        list_duplicates_csv(output_file=args.output, preferred_source_directory=args.prefer_directory)
    elif args.command == 'delete-duplicates':
        delete_duplicates(preferred_source_directory=args.prefer_directory)
    else:
        parser.print_help()