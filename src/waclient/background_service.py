import os
#os.environ["KIVY_NO_CONSOLELOG"] = "1"  # IMPORTANT

import contextlib
import functools
import logging
import os
import threading
from configparser import Error as ConfigParserError

from decorator import decorator
from kivy.config import ConfigParser
from kivy.logger import Logger as logger
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer, ServerClass

from waclient.utilities.logging import CallbackHandler

from waclient.common_config import CONFIG_FILE
from waclient.recording_toolchain import build_recording_toolchain, start_recording_toolchain, stop_recording_toolchain
from waclient.utilities import swallow_exception
from waclient.utilities.osc import get_osc_server, get_osc_client

osc, osc_starter_callback = get_osc_server(is_master=False)

# FIXME what happens if exception on remote OSC endpoint ? CRASH!!
# TODO add custom "local escrow resolver"
# TODO add exception swallowers, and logging pushed to frontend app (if present)

@ServerClass
class BackgroundServer(object):

    """
    The background server automatically starts when service script is launched.

    It must be stopped gracefully with a call to "/stop_server", so that current recordings can be properly stored.

    While the server is alive, recordings can be started and stopped several times without problem.
    """
    _sock = None

    _recording_toolchain = None

    def __init__(self):
        logger.info("Starting service")  # Will not be sent to App (too early)
        osc_starter_callback()  # Opens server port
        self._osc_client = get_osc_client(to_master=True)
        logging.getLogger(None).addHandler(CallbackHandler(self._remote_logging_callback))
        self._termination_event = threading.Event()
        logger.info("Service started")

    def _remote_logging_callback(self, msg):
        return self._osc_client.send_message("/log_output", values=["Service: "+ msg])

    def _send_message(self, address, *values):
        logger.debug("Message sent from service to app: %s", address)
        return self._osc_client.send_message(address, values=values)

    def _load_config(self, filename=CONFIG_FILE):
        logger.info(f"(Re)loading config file {filename}")
        config = ConfigParser()  # No NAME here, sicne named parsers must be Singletons in process!
        try:
            if not os.path.exists(filename):
                raise FileNotFoundError(filename)
            config.read(str(filename))  # Fails silently if file not found
        except ConfigParserError as exc:
            logger.error(f'Service: Ignored missing or corrupted config file {filename}, ignored ({exc!r})')
            raise
        #logger.info(f"Config file {filename} loaded")
        return config

    @osc.address_method('/ping')
    @swallow_exception
    def ping(self):
        logger.info("Ping successful!")
        self._send_message("/log_output", "Pong")

    @osc.address_method('/start_recording')
    @swallow_exception
    def start_recording(self):
        if self.is_recording:
            logger.warning("Ignoring call to service.start_recording(), since recording is already started")
            return
        logger.info("Starting recording")
        if not self._recording_toolchain:
            config = self._load_config()
            self._recording_toolchain = build_recording_toolchain(config)
        start_recording_toolchain(self._recording_toolchain)
        logger.info("Recording started")
        self.broadcast_recording_state()

    @property
    def is_recording(self):
        return bool(self._recording_toolchain and self._recording_toolchain["sensors_manager"].is_running)

    @osc.address_method('/broadcast_recording_state')
    @swallow_exception
    def broadcast_recording_state(self):
        logger.info("Broadcasting recording state")  # TODO make this DEBUG
        self._send_message("/receive_recording_state", self.is_recording)

    @osc.address_method('/stop_recording')
    @swallow_exception
    def stop_recording(self):
        if not self.is_recording:
            logger.warning("Ignoring call to service.stop_recording(), since recording is already stopped")
            return
        logger.info("Stopping recording")
        try:
            stop_recording_toolchain(self._recording_toolchain)
            logger.info("Recording stopped")
        finally:  # Trigger all this even if container flushing failed
            self._recording_toolchain = None  # Will force a reload of config on next recording
            self.broadcast_recording_state()


    @osc.address_method('/stop_server')
    @swallow_exception
    def stop_server(self):
        logger.info("Stopping service")

        if self.is_recording:
            logger.info("Recording is in progress, we stop it as part of service shutdown")
            self.stop_recording()

        osc.stop_all()
        self._termination_event.set()
        logger.info("Service stopped")

    @swallow_exception
    def join(self):
        """
        Wait for the termination of the background server
        (meant for use by the main thread of the service process).
        """
        self._termination_event.wait()


def main():
    logger.info("Service process launches")
    server = BackgroundServer()
    server.join()
    logger.info("Service process exits")


if __name__ == "__main__":
    main()
