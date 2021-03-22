
import json
import re
from ctypes import c_void_p, c_wchar_p
from logging import DEBUG, NullHandler, getLogger
from subprocess import CalledProcessError, check_output

from pymediainfo import MediaInfo
from pymediainfo import __version__ as pymediainfo_version

from .. import VIDEO_EXTENSIONS
from ..properties import (
    AudioChannels,
    AudioCodec,
    AudioCompression,
    AudioProfile,
    Basic,
    BitRateMode,
    Duration,
    Language,
    Quantity,
    ScanType,
    SubtitleFormat,
    VideoCodec,
    VideoEncoder,
    VideoProfile,
    VideoProfileLevel,
    VideoProfileTier,
    YesNo,
)
from ..property import (
    MultiValue,
    Property,
)
from ..provider import (
    MalformedFileError,
    Provider,
)
from ..rules import (
    AtmosRule,
    AudioChannelsRule,
    ClosedCaptionRule,
    DtsHdRule,
    HearingImpairedRule,
    LanguageRule,
    ResolutionRule,
)
from ..units import units
from ..utils import (
    define_candidate,
    detect_os,
)

logger = getLogger(__name__)
logger.addHandler(NullHandler())


WARN_MSG = r'''
=========================================================================================
MediaInfo not found on your system or could not be loaded.
Visit https://mediaarea.net/ to download it.
If you still have problems, please check if the downloaded version matches your system.
To load MediaInfo from a specific location, please define the location as follow:
  knowit --mediainfo /usr/local/mediainfo/lib <video_path>
  knowit --mediainfo /usr/local/mediainfo/bin <video_path>
  knowit --mediainfo "C:\Program Files\MediaInfo" <video_path>
  knowit --mediainfo C:\Software\MediaInfo.dll <video_path>
  knowit --mediainfo C:\Software\MediaInfo.exe <video_path>
  knowit --mediainfo /opt/mediainfo/libmediainfo.so <video_path>
  knowit --mediainfo /opt/mediainfo/libmediainfo.dylib <video_path>
=========================================================================================
'''


class MediaInfoExecutor:
    """Media info executable knows how to execute media info: using ctypes or cli."""

    version_re = re.compile(r'\bv(?P<version>\d+(?:\.\d+)+)\b')

    locations = {
        'unix': ('/usr/local/mediainfo/lib', '/usr/local/mediainfo/bin', '__PATH__'),
        'windows': ('__PATH__', ),
        'macos': ('__PATH__', ),
    }

    def __init__(self, location, version):
        """Initialize the object."""
        self.location = location
        self.version = version

    def extract_info(self, filename):
        """Extract media info."""
        return self._execute(filename)

    def _execute(self, filename):
        raise NotImplementedError

    @classmethod
    def _get_version(cls, output):
        match = cls.version_re.search(output)
        if match:
            version = tuple([int(v) for v in match.groupdict()['version'].split('.')])
            return version

    @classmethod
    def get_executor_instance(cls, suggested_path=None):
        """Return the executor instance."""
        os_family = detect_os()
        logger.debug('Detected os: %s', os_family)
        for exec_cls in (MediaInfoCTypesExecutor, MediaInfoCliExecutor):
            executor = exec_cls.create(os_family, suggested_path)
            if executor:
                return executor


class MediaInfoCliExecutor(MediaInfoExecutor):
    """Media info using cli."""

    names = {
        'unix': ('mediainfo', ),
        'windows': ('MediaInfo.exe', ),
        'macos': ('mediainfo', ),
    }

    def _execute(self, filename):
        return MediaInfo(check_output([self.location, '--Output=JSON', '--Full', filename]).decode())

    @classmethod
    def create(cls, os_family=None, suggested_path=None):
        """Create the executor instance."""
        for candidate in define_candidate(cls.locations, cls.names, os_family, suggested_path):
            try:
                output = check_output([candidate, '--version']).decode()
                version = cls._get_version(output)
                if version:
                    logger.debug('MediaInfo cli detected: %s', candidate)
                    return MediaInfoCliExecutor(candidate, version)
            except CalledProcessError as e:
                # old mediainfo returns non-zero exit code for mediainfo --version
                version = cls._get_version(e.output.decode())
                if version:
                    logger.debug('MediaInfo cli detected: %s', candidate)
                    return MediaInfoCliExecutor(candidate, version)
            except OSError:
                pass


class MediaInfoCTypesExecutor(MediaInfoExecutor):
    """Media info ctypes."""

    names = {
        'unix': ('libmediainfo.so.0', ),
        'windows': ('MediaInfo.dll', ),
        'macos': ('libmediainfo.0.dylib', 'libmediainfo.dylib'),
    }

    def _execute(self, filename):
        # Create a MediaInfo handle
        return MediaInfo.parse(filename, library_file=self.location, output='JSON')

    @classmethod
    def create(cls, os_family=None, suggested_path=None):
        """Create the executor instance."""
        for candidate in define_candidate(cls.locations, cls.names, os_family, suggested_path):
            if MediaInfo.can_parse(candidate):
                lib, handle, lib_version_str, lib_version = MediaInfo._get_library(candidate)
                lib.MediaInfo_Option.argtypes = [c_void_p, c_wchar_p, c_wchar_p]
                lib.MediaInfo_Option.restype = c_wchar_p
                version = MediaInfoExecutor._get_version(lib.MediaInfo_Option(None, "Info_Version", ""))

                logger.debug('MediaInfo library detected: %s (v%s)', candidate, '.'.join(map(str, version)))
                return MediaInfoCTypesExecutor(candidate, version)


class MediaInfoProvider(Provider):
    """Media Info provider."""

    executor = None

    def __init__(self, config, suggested_path):
        """Init method."""
        super().__init__(config, {
            'general': {
                'title': Property('Title', description='media title'),
                'path': Property('CompleteName', description='media path'),
                'duration': Duration('Duration', description='media duration'),
                'size': Quantity('FileSize', units.byte, description='media size'),
                'bit_rate': Quantity('OverallBitRate', units.bps, description='media bit rate'),
            },
            'video': {
                'id': Basic('ID', int, allow_fallback=True, description='video track number'),
                'name': Property('name', description='video track name'),
                'language': Language('Language', description='video language'),
                'duration': Duration('Duration', description='video duration'),
                'size': Quantity('StreamSize', units.byte, description='video stream size'),
                'width': Quantity('Width', units.pixel),
                'height': Quantity('Height', units.pixel),
                'scan_type': ScanType(config, 'ScanType', default='Progressive', description='video scan type'),
                'aspect_ratio': Basic('DisplayAspectRatio', float, description='display aspect ratio'),
                'pixel_aspect_ratio': Basic('PixelAspectRatio', float, description='pixel aspect ratio'),
                'resolution': None,  # populated with ResolutionRule
                'frame_rate': Quantity('FrameRate', units.FPS, float, description='video frame rate'),
                # frame_rate_mode
                'bit_rate': Quantity('BitRate', units.bps, description='video bit rate'),
                'bit_depth': Quantity('BitDepth', units.bit, description='video bit depth'),
                'codec': VideoCodec(config, 'CodecID', description='video codec'),
                'profile': VideoProfile(config, 'Format_Profile', description='video codec profile'),
                'profile_level': VideoProfileLevel(config, 'Format_Level', description='video codec profile level'),
                'profile_tier': VideoProfileTier(config, 'Format_Profile', description='video codec profile tier'),
                'encoder': VideoEncoder(config, 'encoded_library_name', description='video encoder'),
                'media_type': Property('InternetMediaType', description='video media type'),
                'forced': YesNo('Forced', hide_value=False, description='video track forced'),
                'default': YesNo('Default', hide_value=False, description='video track default'),
            },
            'audio': {
                'id': Basic('ID', int, allow_fallback=True, description='audio track number'),
                'name': Property('Title', description='audio track name'),
                'language': Language('Language', description='audio language'),
                'duration': Duration('Duration', description='audio duration'),
                'size': Quantity('StreamSize', units.byte, description='audio stream size'),
                'codec': MultiValue(AudioCodec(config, 'CodecID', description='audio codec')),
                'profile': MultiValue(AudioProfile(config, 'Format_Profile', description='audio codec profile'),
                                      delimiter=' / '),
                'channels_count': MultiValue(AudioChannels('Channels', description='audio channels count')),
                'channel_positions': MultiValue(name='other_channel_positions', handler=(lambda x, *args: x),
                                                delimiter=' / ', private=True, description='audio channels position'),
                'channels': None,  # populated with AudioChannelsRule
                'bit_depth': Quantity('BitDepth', units.bit, description='audio bit depth'),
                'bit_rate': MultiValue(Quantity('BitRate', units.bps, description='audio bit rate')),
                'bit_rate_mode': MultiValue(BitRateMode(config, 'BitRateMode', description='audio bit rate mode')),
                'sampling_rate': MultiValue(Quantity('SamplingRate', units.Hz, description='audio sampling rate')),
                'compression': MultiValue(AudioCompression(config, 'Compression_Mode',
                                                           description='audio compression')),
                'forced': YesNo('Forced', hide_value=False, description='audio track forced'),
                'default': YesNo('Default', hide_value=False, description='audio track default'),
            },
            'subtitle': {
                'id': Basic('ID', int, allow_fallback=True, description='subtitle track number'),
                'name': Property('Title', description='subtitle track name'),
                'language': Language('Language', description='subtitle language'),
                'hearing_impaired': None,  # populated with HearingImpairedRule
                '_closed_caption': Property('captionservicename', private=True),
                'closed_caption': None,  # populated with ClosedCaptionRule
                'format': SubtitleFormat(config, 'CodecID', description='subtitle format'),
                'forced': YesNo('Forced', hide_value=False, description='subtitle track forced'),
                'default': YesNo('Default', hide_value=False, description='subtitle track default'),
            },
        }, {
            'video': {
                'language': LanguageRule('video language'),
                'resolution': ResolutionRule('video resolution'),
            },
            'audio': {
                'language': LanguageRule('audio language'),
                'channels': AudioChannelsRule('audio channels'),
                '_atmosrule': AtmosRule('atmos rule'),
                '_dtshdrule': DtsHdRule('dts-hd rule'),
            },
            'subtitle': {
                'language': LanguageRule('subtitle language'),
                'hearing_impaired': HearingImpairedRule('subtitle hearing impaired'),
                'closed_caption': ClosedCaptionRule('closed caption'),
            }
        })
        self.executor = MediaInfoExecutor.get_executor_instance(suggested_path)

    def accepts(self, video_path):
        """Accept any video when MediaInfo is available."""
        if self.executor is None:
            logger.warning(WARN_MSG)
            self.executor = False

        return self.executor and video_path.lower().endswith(VIDEO_EXTENSIONS)

    def describe(self, video_path, context):
        """Return video metadata."""
        media_info = self.executor.extract_info(video_path)
        data = json.loads(media_info)

        def debug_data():
            """Debug data."""
            return json.dumps(data, indent=4)

        context['debug_data'] = debug_data

        if logger.isEnabledFor(DEBUG):
            logger.debug('Video %r scanned using mediainfo %r has raw data:\n%s',
                         video_path, self.executor.location, debug_data())

        result = {}
        tracks = data.get('media', {}).get('track', [])
        if tracks:
            general_tracks = []
            video_tracks = []
            audio_tracks = []
            subtitle_tracks = []
            for track in tracks:
                track_type = track.get('@type')
                if track_type == 'General':
                    general_tracks.append(track)
                elif track_type == 'Video':
                    video_tracks.append(track)
                elif track_type == 'Audio':
                    audio_tracks.append(track)
                elif track_type == 'Text':
                    subtitle_tracks.append(track)

            result = self._describe_tracks(video_path, general_tracks[0] if general_tracks else {},
                                           video_tracks, audio_tracks, subtitle_tracks, context)
        if not result:
            raise MalformedFileError

        result['provider'] = {
            'name': 'mediainfo',
            'version': self.version
        }

        return result

    @property
    def version(self):
        """Return mediainfo version information."""
        versions = {'pymediainfo': pymediainfo_version}
        if self.executor:
            executor_version = '.'.join(map(str, self.executor.version))
            versions[self.executor.location] = f'v{executor_version}'
        return versions
