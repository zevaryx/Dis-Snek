import os
import sys
import array
import ctypes
import ctypes.util
import typing
from email.policy import default
from enum import IntEnum
from typing import Type, Any

import attr

from dis_snek.client.const import MISSING

c_int_ptr = ctypes.POINTER(ctypes.c_int)
c_int16_ptr = ctypes.POINTER(ctypes.c_int16)
c_float_ptr = ctypes.POINTER(ctypes.c_float)

lib_opus = MISSING


class EncoderStructure(ctypes.Structure):
    ...


EncoderStructurePointer = ctypes.POINTER(EncoderStructure)


def error_lt(result, func, args) -> int:
    if result < 0:
        raise Exception
    return result


def error_ne(result, func, args) -> int:
    # noinspection PyProtectedMember
    ret = args[-1]._obj
    if ret.value != 0:
        raise Exception
    return result


@attr.s(auto_attribs=True)
class FuncData:
    arg_types: Any = attr.ib()
    res_type: Any = attr.ib()
    err_check: Any = attr.ib(default=None)


# opus consts
# from https://github.com/xiph/opus/blob/master/include/opus_defines.h


class OpusStates(IntEnum):
    OK = 0
    """No Error!"""
    BAD_ARG = -1
    """One or more invalid/out of range arguments"""
    BUFFER_TOO_SMALL = -2
    """Not enough bytes allocated in the buffer"""
    INTERNAL_ERROR = -3
    """An internal error was detected"""
    INVALID_PACKET = -4
    """The compressed data passed is corrupted"""
    UNIMPLEMENTED = -5
    """Invalid/unsupported request number"""
    INVALID_STATE = -6
    """An encoder or decoder structure is invalid or already freed"""
    ALLOC_FAIL = -7
    """Memory allocation has failed"""


class EncoderCTL(IntEnum):
    OK = 0
    APPLICATION_AUDIO = 2049
    APPLICATION_VOIP = 2048
    APPLICATION_LOWDELAY = 2051
    CTL_SET_BITRATE = 4002
    CTL_SET_BANDWIDTH = 4008
    CTL_SET_FEC = 4012
    CTL_SET_PLP = 4014
    CTL_SET_SIGNAL = 4024


class DecoderCTL(IntEnum):
    CTL_SET_GAIN = 4034
    CTL_LAST_PACKET_DURATION = 4039


class BandCTL(IntEnum):
    NARROW = 1101
    MEDIUM = 1102
    WIDE = 1103
    SUPERWIDE = 1104
    FULL = 1105


class SignalCTL(IntEnum):
    AUTO = -1000
    VOICE = 3001
    MUSIC = 3002


exported_functions: dict[str, FuncData] = {
    "opus_strerror": FuncData([ctypes.c_int], ctypes.c_char_p),
    "opus_encoder_get_size": FuncData([ctypes.c_int], ctypes.c_int),
    "opus_encoder_create": FuncData(
        [ctypes.c_int, ctypes.c_int, ctypes.c_int, c_int_ptr],
        EncoderStructurePointer,
        error_ne,
    ),
    "opus_encode": FuncData(
        [
            EncoderStructurePointer,
            c_int16_ptr,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int32,
        ],
        ctypes.c_int32,
        error_lt,
    ),
    "opus_encoder_ctl": FuncData(None, ctypes.c_int32, error_lt),
    "opus_encoder_destroy": FuncData([EncoderStructurePointer], None, None),
}


class Encoder:
    def __init__(self):
        if sys.platform == "win32":
            architecture = "x64" if sys.maxsize > 32**2 else "x86"
            directory = os.path.dirname(os.path.abspath(__file__))
            name = os.path.join(directory, "../../bin", f"opus-{architecture}.dll")
        else:
            name = ctypes.util.find_library("opus")

        self.lib_opus = ctypes.cdll.LoadLibrary(name)

        for func_name, opt in exported_functions.items():
            func = getattr(self.lib_opus, func_name)

            func.restype = opt.res_type

            if opt.arg_types:
                func.argtypes = opt.arg_types

            if opt.err_check:
                func.errcheck = opt.err_check

        self.sample_rate: int = 48000  # bps
        self.channels: int = 2
        self.frame_length: int = 20  # ms
        self.sample_size: int = 4
        self.expected_packet_loss: float = 0
        self.bitrate: int = 64

        self.encoder = self.create_state()
        self.set_bitrate(self.bitrate)
        self.set_fec(True)
        self.set_expected_pack_loss(self.expected_packet_loss)
        self.set_bandwidth("FULL")
        self.set_signal_type("AUTO")

    def __del__(self):
        if self.encoder:
            self.lib_opus.opus_encoder_destroy(self.encoder)
            self.encoder = None

    @property
    def samples_per_frame(self) -> int:
        return int(self.sample_rate / 1000 * self.frame_length)

    @property
    def delay(self) -> float:
        return self.frame_length / 1000

    @property
    def frame_size(self) -> int:
        return self.samples_per_frame * self.channels * 2

    def create_state(self) -> EncoderStructurePointer:
        """Create an opus encoder state."""
        ret = ctypes.c_int()
        return self.lib_opus.opus_encoder_create(self.sample_rate, 2, EncoderCTL.APPLICATION_AUDIO, ctypes.byref(ret))

    def set_bitrate(self, kbps: int) -> None:
        """Set the birate of the opus encoder"""
        self.bitrate = min(512, max(16, kbps))
        self.lib_opus.opus_encoder_ctl(self.encoder, EncoderCTL.CTL_SET_BITRATE, self.bitrate * 1024)

    def set_signal_type(self, sig_type: str) -> None:
        """Set the signal type to encode"""
        try:
            sig_type = SignalCTL[sig_type.upper()]
        except KeyError as e:
            raise ValueError(f"`{sig_type}` is not a valid signal type. Please consult documentation") from e

        self.lib_opus.opus_encoder_ctl(self.encoder, EncoderCTL.CTL_SET_SIGNAL, sig_type)

    def set_bandwidth(self, bandwidth_type: str) -> None:
        """Set the bandwidth for the encoder"""
        try:
            bandwidth_type = BandCTL[bandwidth_type.upper()]
        except KeyError as e:
            raise ValueError(f"`{bandwidth_type}` is not a valid bandwidth type. Please consult documentation") from e
        self.lib_opus.opus_encoder_ctl(self.encoder, EncoderCTL.CTL_SET_BANDWIDTH, bandwidth_type)

    def set_fec(self, enabled: bool) -> None:
        """Enable or disable the forward error correction"""
        self.lib_opus.opus_encoder_ctl(self.encoder, EncoderCTL.CTL_SET_FEC, int(enabled))

    def set_expected_pack_loss(self, expected_packet_loss: float) -> None:
        """Set the expected packet loss amount"""
        self.expected_packet_loss = expected_packet_loss
        self.lib_opus.opus_encoder_ctl(self.encoder, EncoderCTL.CTL_SET_PLP, self.expected_packet_loss)

    def encode(self, pcm: bytes) -> bytes:
        """todo: doc"""
        max_data_bytes = len(pcm)
        pcm = ctypes.cast(pcm, c_int16_ptr)
        data = (ctypes.c_char * max_data_bytes)()
        resp = self.lib_opus.opus_encode(self.encoder, pcm, self.samples_per_frame, data, max_data_bytes)
        return array.array("b", data[:resp]).tobytes()
