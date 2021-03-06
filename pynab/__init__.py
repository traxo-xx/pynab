#!/usr/bin/env python
# -*- coding: utf-8 -*-

__author__ = 'James Meneghello'
__email__ = 'murodese@gmail.com'
__version__ = '1.1.0'

import logging
import config
import logging.handlers
import os
import colorlog
import sys


log = logging.getLogger(__name__)
log.setLevel(config.log.get('logging_level', logging.DEBUG))

logging_file = config.log.get('logging_file')
log_descriptor = None

formatter = colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s - %(levelname)s - %(reset)s %(blue)s%(message)s",
    datefmt=None,
    reset=True,
    log_colors={
        'DEBUG':    'cyan',
        'INFO':     'green',
        'WARNING':  'yellow',
        'ERROR':    'red',
        'CRITICAL': 'red',
    }
)

if logging_file:
    name, _ = os.path.splitext(os.path.basename(sys.argv[0].rstrip(os.sep)))
    file, ext = os.path.splitext(config.log.get('logging_file'))
    logging_file = ''.join([file, '_', name, ext])

    handler = logging.handlers.RotatingFileHandler(logging_file, maxBytes=config.log.get('max_log_size', 50*1024*1024), backupCount=5, encoding='utf-8')
    #handler.setFormatter(formatter)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    log.addHandler(handler)
    log_descriptor = handler.stream.fileno()
else:
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    log.addHandler(handler)

# set up root_dir for use with templates etc
root_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
