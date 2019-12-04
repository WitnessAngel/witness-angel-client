
import sys, logging

# For now allow EVERYTHING
logging.getLogger(None).setLevel(logging.DEBUG)
logging.disable(0)

custom_kivy_stream_handler = logging.StreamHandler()
sys._kivy_logging_handler = custom_kivy_stream_handler

from kivy.logger import Logger

# Finish ugly monkey-patching by Kivy
assert logging.getLogger("kivy") is logging.root
logging.Logger.root = logging.root
logging.Logger.manager.root = logging.root

#import logging_tree
#logging_tree.printout()
