# -*- encoding: utf-8 -*-
# @Time    :   2020/12/7
# @Author  :   Xiaolei Wang
# @email   :   wxl1999@foxmail.com

# UPDATE
# @Time    :   2020/12/7
# @Author  :   Xiaolei Wang
# @email   :   wxl1999@foxmail.com

import hashlib
import os
import shutil
import time

import datetime
import requests
import tqdm
from loguru import logger


class DownloadableFile:
    """
    A class used to abstract any file that has to be downloaded online.

    Any task that needs to download a file needs to have a list RESOURCES
    that have objects of this class as elements.

    This class provides the following functionality:

    - Download a file from a URL
    - Untar the file if zipped
    - Checksum for the downloaded file

    An object of this class needs to be created with:

    - url <string> : URL or Google Drive id to download from
    - file_name <string> : File name that the file should be named
    - hashcode <string> : SHA256 hashcode of the downloaded file
    - zipped <boolean> : False if the file is not compressed
    - from_google <boolean> : True if the file is from Google Drive
    """

    def __init__(self, url, file_name, hashcode, zipped=True, from_google=False):
        self.url = url
        self.file_name = file_name
        self.hashcode = hashcode
        self.zipped = zipped
        self.from_google = from_google

    def checksum(self, dpath):
        """
        Checksum on a given file.

        :param dpath: path to the downloaded file.
        """
        sha256_hash = hashlib.sha256()
        with open(os.path.join(dpath, self.file_name), "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):
                sha256_hash.update(byte_block)
            if sha256_hash.hexdigest() != self.hashcode:
                # remove_dir(dpath)
                raise AssertionError(
                    f"[ Checksum for {self.file_name} from \n{self.url}\n"
                    "does not match the expected checksum. Please try again. ]"
                )
            else:
                logger.debug("Checksum Successful")

    def download_file(self, dpath):
        if self.from_google:
            download_from_google_drive(self.url, os.path.join(dpath, self.file_name))
        else:
            download(self.url, dpath, self.file_name)

        self.checksum(dpath)

        if self.zipped:
            untar(dpath, self.file_name)


def download(url, path, fname, redownload=False, num_retries=5):
    """
    Download file using `requests`.
    If `redownload` is set to False, will not download file again if it is present (default False).
    """
    outfile = os.path.join(path, fname)
    download = not os.path.exists(outfile) or redownload
    if not download:
        return

    logger.info(f"Downloading {url} to {outfile}")
    retry = num_retries
    exp_backoff = [2 ** r for r in reversed(range(retry))]
    pbar = tqdm.tqdm(unit='B', unit_scale=True, desc='Downloading {}'.format(fname))

    while download and retry > 0:
        try:
            response = make_request(url)
            total_size = int(response.headers.get('Content-Length', -1))
            pbar.total = total_size
            done = write_chunks(response, outfile, pbar, total_size)
            if done >= total_size:
                break
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            retry -= 1
            handle_retry(retry, exp_backoff, pbar)
        finally:
            if response:
                response.close()

    if retry <= 0:
        raise RuntimeError('Connection broken too many times. Stopped retrying.')

    validate_download(pbar, total_size, done, url)
    pbar.close()

def make_request(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36 Edg/87.0.664.60',
    }
    return requests.get(url, stream=True, headers=headers)

def write_chunks(response, outfile, pbar, total_size):
    CHUNK_SIZE = 32768
    done = 0
    with open(outfile, 'wb') as f:
        for chunk in response.iter_content(CHUNK_SIZE):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                pbar.update(len(chunk))
                if total_size < done:
                    total_size = done
                    pbar.total = total_size
    return done

def handle_retry(retry, exp_backoff, pbar):
    pbar.clear()
    if retry > 0:
        logger.debug(f'Connection error, retrying. ({retry} retries left)')
        time.sleep(exp_backoff[retry])

def validate_download(pbar, total_size, done, url):
    _ = pbar # This is a no-op, just to use the variable to avoid conventional bugs
    if done < total_size:
        raise RuntimeError(f'Received less data than specified in Content-Length header for {url}. There may be a download problem.')


def _get_confirm_token(response):
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            return value
    return None


def download_from_google_drive(gd_id, destination):
    """
    Use the requests package to download a file from Google Drive.
    """
    URL = 'https://docs.google.com/uc?export=download'

    with requests.Session() as session:
        response = session.get(URL, params={'id': gd_id}, stream=True)
        token = _get_confirm_token(response)

        if token:
            response.close()
            params = {'id': gd_id, 'confirm': token}
            response = session.get(URL, params=params, stream=True)

        CHUNK_SIZE = 32768
        with open(destination, 'wb') as f:
            for chunk in response.iter_content(CHUNK_SIZE):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
        response.close()


def move(path1, path2):
    """
    Rename the given file.
    """
    shutil.move(path1, path2)


def untar(path, fname, delete_tar=True): #change deleteTar -> delete_tar
    """
    Unpack the given archive file to the same directory.

    :param str path:
        The folder containing the archive. Will contain the contents.

    :param str fname:
        The filename of the archive file.

    :param bool delete_tar:
        If true, the archive will be deleted after extraction.
    """
    logger.debug(f'unpacking {fname}')
    fullpath = os.path.join(path, fname)
    shutil.unpack_archive(fullpath, path)
    if delete_tar:
        os.remove(fullpath)


def make_dir(path):
    """
    Make the directory and any nonexistent parent directories (`mkdir -p`).
    """
    # the current working directory is a fine path
    if path != '':
        os.makedirs(path, exist_ok=True)


def remove_dir(path):
    """
    Remove the given directory, if it exists.
    """
    shutil.rmtree(path, ignore_errors=True)


BUILT_FLAG = '.built'

def check_build(path, version_string=None):
    """
    Check if '.built' flag has been set for that task.
    If a version_string is provided, this has to match, or the version is regarded as
    not built.
    """
    fname = os.path.join(path, BUILT_FLAG)
    if version_string:
        if not os.path.isfile(fname):
            return False
        else:
            with open(fname, 'r') as read:
                text = read.read().split('\n')
            return len(text) > 1 and text[1] == version_string
    else:
        return os.path.isfile(fname)

def mark_done(path, version_string=None):
    """
    Mark this path as prebuilt.
    Marks the path as done by adding a '.built' file with the current timestamp
    plus a version description string if specified.
    :param str path:
        The file path to mark as built.
    :param str version_string:
        The version of this dataset.
    """
    fname = os.path.join(path, BUILT_FLAG)
    with open(fname, 'w') as write:
        write.write(str(datetime.datetime.today()))
        if version_string:
            write.write('\n' + version_string)

def build(dpath, dfile, version=None):
    if not check_build(dpath, version):
        logger.info('[Building data: ' + dpath + ']')
        if check_build(dpath):
            remove_dir(dpath)
        make_dir(dpath)
        # Download the data.
        downloadable_file = dfile
        downloadable_file.download_file(dpath)
        mark_done(dpath, version)
