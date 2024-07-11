import requests
import config


class Logwatch:
    """Class for sending logging messages to the logwatch server'"""

    def __init__(self, channel: str):
        self.drain = config.FAIRHUB_CATCH_ALL_LOG_ENDPOINT
        self.triton_drain = config.FAIRHUB_TRITON_LOG_ENDPOINT
        self.maestro2_drain = config.FAIRHUB_MAESTRO2_LOG_ENDPOINT
        self.eidon_drain = config.FAIRHUB_EIDON_LOG_ENDPOINT
        self.ecg_drain = config.FAIRHUB_ECG_LOG_ENDPOINT
        self.cgm_drain = config.FAIRHUB_CGM_LOG_ENDPOINT

        if (channel == "triton") and (self.triton_drain is not None):
            self.drain = self.triton_drain
        if (channel == "maestro2") and (self.maestro2_drain is not None):
            self.drain = self.maestro2_drain
        if (channel == "eidon") and (self.eidon_drain is not None):
            self.drain = self.eidon_drain
        if (channel == "ecg") and (self.ecg_drain is not None):
            self.drain = self.ecg_drain
        if (channel == "cgm") and (self.cgm_drain is not None):
            self.drain = self.cgm_dr

    def info(self, message: str):
        """Send an info message to the logwatch server"""
        requests.post(self.drain, json={"level": "info", "message": message})

    def error(self, message: str):
        """Send an error message to the logwatch server"""
        requests.post(self.drain, json={"level": "error", "message": message})

    def warn(self, message: str):
        """Send a warning message to the logwatch server"""
        requests.post(self.drain, json={"level": "warning", "message": message})

    def debug(self, message: str):
        """Send a debug message to the logwatch server"""
        requests.post(self.drain, json={"level": "debug", "message": message})

    def critical(self, message: str):
        """Send a critical message to the logwatch server"""
        requests.post(self.drain, json={"level": "critical", "message": message})

    def trace(self, message: str):
        """Send a trace message to the logwatch server"""
        requests.post(self.drain, json={"level": "trace", "message": message})