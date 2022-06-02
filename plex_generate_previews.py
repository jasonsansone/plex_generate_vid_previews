#!/usr/bin/env python3

# EDIT These Vars #
PLEX_URL = 'https://xxxxxx.plex.direct:32400/' # If running locally, can also enter IP directly "https://127.0.0.1:32400/"
PLEX_TOKEN = 'xxxxxx'
PLEX_BIF_FRAME_INTERVAL = 5
THUMBNAIL_QUALITY = 4 # Allowed range is 2 - 6 with 2 being highest quality and largest file size and 6 being lowest quality and smallest file size. # 
PLEX_LOCAL_MEDIA_PATH = '/path_to/plex/Library/Application Support/Plex Media Server/Media'
TMP_FOLDER = '/dev/shm/plex_generate_previews'
GPU_THREADS = 4
CPU_THREADS = 4

# DO NOT EDIT BELOW HERE #

import os
import sys
from concurrent.futures import ProcessPoolExecutor
import re
import subprocess
import shutil
import multiprocessing
import glob
import os
import struct
if not os.path.isfile('/usr/bin/mediainfo'):
    print('MediaInfo not found.  MediaInfo must be installed and available in PATH.')
    sys.exit(1)	
try:
    from pymediainfo import MediaInfo
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install pymediainfo".')
    sys.exit(1)
try:
    import gpustat
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install gpustat".')
    sys.exit(1)
import time
try:
    import requests
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install requests".')
    sys.exit(1)
import array
try:
    from plexapi.server import PlexServer
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install plexapi".')
    sys.exit(1)
try:
    from loguru import logger
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install loguru".')
    sys.exit(1)
try:
    from rich.console import Console
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install rich".')
    sys.exit(1)
try:
    from rich.progress import Progress, SpinnerColumn, MofNCompleteColumn
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install rich".')
    sys.exit(1)
if not os.path.isfile('/usr/bin/ffmpeg'):
    print('FFmpeg not found.  FFmpeg must be installed and available in PATH.')
    sys.exit(1)

console = Console(color_system=None, stderr=True)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def generate_images(video_file, output_folder, lock):
    media_info = MediaInfo.parse(video_file)
    vf_parameters = "fps=fps={}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))
    dynamic_range = "SDR"
    
    # Check if we have a HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
        vf_parameters = "fps=fps={}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))
        dynamic_range = "HDR"
        
    args = [
        "/usr/bin/ffmpeg", "-loglevel", "info", "-skip_frame:v", "nokey", "-threads:0", "1", "-i",
        video_file, "-an", "-sn", "-dn", "-q:v", str(THUMBNAIL_QUALITY),
        "-vf",
        vf_parameters, '{}/img-%06d.jpg'.format(output_folder)
    ]

    start = time.time()
    hw = False

    with lock:
        gpu = gpustat.core.new_query()[0]
        gpu_ffmpeg = [c for c in gpu.processes if c["command"].lower() == 'ffmpeg']
        if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
            hw = True
            args.insert(5, "-hwaccel")
            args.insert(6, "cuda")

        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Allow time for it to start
        time.sleep(1)

    out, err = proc.communicate()
    if proc.returncode != 0:
        err_lines = err.decode('utf-8').split('\n')[-5:]
        logger.error(err_lines)
        raise Exception('Problem trying to generate images for {}'.format(os.path.basename(video_file)))

    # Speed
    end = time.time()
    seconds = round(end - start, 1)
    speed = re.findall('speed= ?([0-9]+\.?[0-9]*|\.[0-9]+)x', err.decode('utf-8'))
    if speed:
        speed = speed[-1]
    logger.info('Generated Video Preview for {}\n| HW={} | {} | TIME={}s | SPEED={}x |'.format(os.path.basename(video_file), hw, dynamic_range, seconds, speed))

    # Optimize and Rename Images
    for image in glob.glob('{}/img*.jpg'.format(output_folder)):
        frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
        frame_second = frame_no * PLEX_BIF_FRAME_INTERVAL
        os.rename(image, os.path.join(output_folder, '{:010d}.jpg'.format(frame_second)))


def generate_bif(bif_filename, images_path):
    """
    Build a .bif file
    @param bif_filename name of .bif file to create
    @param images_path Directory of image files 00000001.jpg
    """
    magic = [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
    version = 0

    images = [img for img in os.listdir(images_path) if os.path.splitext(img)[1] == '.jpg']
    images.sort()

    f = open(bif_filename, "wb")
    array.array('B', magic).tofile(f)
    f.write(struct.pack("<I", version))
    f.write(struct.pack("<I", len(images)))
    f.write(struct.pack("<I", 1000 * PLEX_BIF_FRAME_INTERVAL))
    array.array('B', [0x00 for x in range(20, 64)]).tofile(f)

    bif_table_size = 8 + (8 * len(images))
    image_index = 64 + bif_table_size
    timestamp = 0

    # Get the length of each image
    for image in images:
        statinfo = os.stat(os.path.join(images_path, image))
        f.write(struct.pack("<I", timestamp))
        f.write(struct.pack("<I", image_index))
        timestamp += 1
        image_index += statinfo.st_size

    f.write(struct.pack("<I", 0xffffffff))
    f.write(struct.pack("<I", image_index))

    # Now copy the images
    for image in images:
        data = open(os.path.join(images_path, image), "rb").read()
        f.write(data)

    f.close()


def process_item(item_key, lock):
    sess = requests.Session()
    sess.verify = False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=sess)

    data = plex.query('{}/tree'.format(item_key))

    for media_part in data.findall('.//MediaPart'):
        if 'hash' in media_part.attrib:
            # Filter Processing by HDD Path
            if len(sys.argv) > 1:
                if sys.argv[1] not in media_part.attrib['file']:
                    return
            bundle_hash = media_part.attrib['hash']
            bundle_file = '{}/{}{}'.format(bundle_hash[0], bundle_hash[1::1], '.bundle')
            bundle_path = os.path.join(PLEX_LOCAL_MEDIA_PATH, bundle_file)
            indexes_path = os.path.join(bundle_path, 'Contents', 'Indexes')
            index_bif = os.path.join(indexes_path, 'index-sd.bif')
            tmp_path = os.path.join(TMP_FOLDER, bundle_hash)            
            if (not os.path.isfile(index_bif)) and (not os.path.isdir(tmp_path)):
                if not os.path.isdir(indexes_path):
                    os.mkdir(indexes_path)
                try:
                    os.mkdir(tmp_path)
                    generate_images(media_part.attrib['file'], tmp_path, lock)
                    generate_bif(index_bif, tmp_path)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    if os.path.exists(index_bif):
                        os.remove(index_bif)
                    logger.error(e)
                finally:
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)


def run():
    process_pool = ProcessPoolExecutor(max_workers=CPU_THREADS + GPU_THREADS)

    # Ignore SSL Errors
    sess = requests.Session()
    sess.verify = False

    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=sess)

    # Refresh Library before Processing
    logger.info("Beginning library scan")
    plex.library.update()
    while len(plex.activities) != 0:
        logger.info("Waiting on library scan to complete...")
        time.sleep(30)
    logger.info("Library scan complete")
    logger.info("Beginning to clean bundles")
    plex.library.cleanBundles()
    while len(plex.activities) != 0:
        logger.info("Waiting on bundle cleaning to complete...")
        time.sleep(30)
    logger.info("Cleaning bundles complete")
    
    # Get Media from Plex
    logger.info('Getting Media from Plex')
    videos = [m.key for m in plex.library.search(libtype='movie')]
    videos = videos + [m.key for m in plex.library.search(libtype='episode')]
    videos.sort(reverse=True)
    logger.info('Got {} Media from Plex', len(videos))

    m = multiprocessing.Manager()
    lock = m.Lock()

    futures = [process_pool.submit(process_item, key, lock) for key in videos]
    with Progress(SpinnerColumn(), *Progress.get_default_columns(), MofNCompleteColumn(), console=console) as progress:
        for future in progress.track(futures):
            future.result()

    process_pool.shutdown()


if __name__ == '__main__':
    logger.remove()  # Remove default 'stderr' handler
    # We need to specify end=''" as log message already ends with \n (thus the lambda function)
    # Also forcing 'colorize=True' otherwise Loguru won't recognize that the sink support colors
    logger.add(lambda m: console.print('\n%s' % m, end=""), colorize=True, format="{message}")

    if not os.path.exists(PLEX_LOCAL_MEDIA_PATH):
        logger.error('%s does not exist, please edit PLEX_LOCAL_MEDIA_PATH variable' % PLEX_LOCAL_MEDIA_PATH)
        exit(1)

    if 'xxxxxx' in PLEX_URL:
        logger.error('Please update the PLEX_URL variable within this script')
        exit(1)

    if 'xxxxxx' in PLEX_TOKEN:
        logger.error('Please update the PLEX_TOKEN variable within this script')
        exit(1)

     try:
        # Clean TMP Folder
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)   
        os.mkdir(TMP_FOLDER)
        run()
    finally:
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
