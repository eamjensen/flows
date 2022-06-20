"""
Load image code.
"""
from __future__ import annotations
import numpy as np
import warnings
import logging
import astropy.units as u
import astropy.coordinates as coords
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from typing import Tuple, Union, Dict, Any, Optional
from tendrils import api

from dataclasses import dataclass  # , field
import typing
from abc import ABC, abstractmethod

from .filters import FILTERS

logger = logging.getLogger(__name__)  # Singleton logger instance


@dataclass
class InstrumentDefaults:
    """
    Default radius and FWHM for an instrument in arcseconds.
    """
    radius: float = 10
    fwhm: float = 6.0   # Best initial guess
    fwhm_min: float = 3.5
    fwhm_max: float = 18.0


@dataclass
class FlowsImage:
    image: np.ndarray
    header: typing.Dict
    mask: Optional[np.ndarray] = None
    peakmax: Optional[float] = None
    exptime: Optional[float] = None
    instrument_defaults: Optional[InstrumentDefaults] = None
    site: Optional[Dict[str, Any]] = None
    obstime: Optional[Time] = None
    photfilter: Optional[str] = None
    wcs: Optional[WCS] = None

    clean: Optional[np.ma.MaskedArray] = None
    subclean: Optional[np.ma.MaskedArray] = None
    error: Optional[np.ma.MaskedArray] = None

    def __post_init__(self):
        self.shape = self.image.shape
        self.wcs = self.create_wcs()
        # Make empty mask
        if self.mask is None:
            self.mask = np.zeros_like(self.image, dtype='bool')
        self.check_finite()

    def check_finite(self):
        self.mask |= ~np.isfinite(self.image)

    def update_mask(self, mask):
        self.mask = mask
        self.check_finite()

    def create_wcs(self) -> WCS:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=FITSFixedWarning)
            return WCS(header=self.header, relax=True)

    def create_masked_image(self):
        """Warning: this is destructive and will overwrite image data setting masked values to NaN"""
        self.image[self.mask] = np.NaN
        self.clean = np.ma.masked_array(data=self.image, mask=self.mask, copy=False)

    def set_edge_rows_to_value(self, y: Tuple[float] = None, value: typing.Union[int, float, np.float64] = 0):
        if y is None:
            pass
        for row in y:
            self.image[row] = value

    def set_edge_columns_to_value(self, x: Tuple[float] = None, value: typing.Union[int, float, np.float64] = 0):
        if x is None:
            pass
        for col in x:
            self.image[:, col] = value

    @staticmethod
    def get_edge_mask(img: np.ndarray, value: typing.Union[int, float, np.float64] = 0):
        """
        Create boolean mask of given value near edge of image.

        Parameters:
            img (ndarray): image with values for masking.
            value (float): Value to detect near edge. Default=0.

        Returns:
            ndarray: Pixel mask with given values on the edge of image.

        .. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
        """

        mask1 = (img == value)
        mask = np.zeros_like(img, dtype='bool')

        # Mask entire rows and columns which are only the value:
        mask[np.all(mask1, axis=1), :] = True
        mask[:, np.all(mask1, axis=0)] = True

        # Detect "uneven" edges column-wise in image:
        a = np.argmin(mask1, axis=0)
        b = np.argmin(np.flipud(mask1), axis=0)
        for col in range(img.shape[1]):
            if mask1[0, col]:
                mask[:a[col], col] = True
            if mask1[-1, col]:
                mask[-b[col]:, col] = True

        # Detect "uneven" edges row-wise in image:
        a = np.argmin(mask1, axis=1)
        b = np.argmin(np.fliplr(mask1), axis=1)
        for row in range(img.shape[0]):
            if mask1[row, 0]:
                mask[row, :a[row]] = True
            if mask1[row, -1]:
                mask[row, -b[row]:] = True

        return mask

    def apply_edge_mask(self, y: Tuple[int] = None, x: Tuple[int] = None, apply_existing_mask_first: bool = False):
        """
        Masks given rows and columns of image but will replace the current mask! Set apply_existing_mask_first to True
        if the current mask should be kept.
        :param y: Tuple[int] of rows to mask
        :param x: Tuple[int] of columns to mask
        :param apply_existing_mask_first: Whether to apply the existing mask to image first, before overwriting mask.
        :return: None
        """
        if y is None and x is None:
            logger.debug("(y,x) was None when applying edge mask. Edge was not actually masked.")

        if apply_existing_mask_first:
            self.create_masked_image()

        if y is not None:
            self.set_edge_rows_to_value(y=y)

        if x is not None:
            self.set_edge_columns_to_value(x=x)

        self.mask = self.get_edge_mask(self.image)
        self.create_masked_image()


class AbstractInstrument(ABC):
    peakmax: int = None

    @abstractmethod
    def __init__(self):
        pass

    @abstractmethod
    def get_site(self):
        pass

    @abstractmethod
    def get_exptime(self):
        pass

    @abstractmethod
    def get_obstime(self):
        pass

    @abstractmethod
    def get_photfilter(self):
        pass

    @abstractmethod
    def process_image(self):
        pass


@dataclass
class UniqueHeader:
    telescope: str = ''  # Fits Header name of TELESCOP
    instrument: str = ''  # Fits Header name of Instrument (can be partial)
    origin: str = ''  # Fits Header value of ORIGIN (if relevant)
    unique_headers: Optional[Dict[str, Any]] = None  # Unique key value pairs from header for identifying instrument.


class Instrument(AbstractInstrument):
    peakmax: int = None
    siteid: int = None
    telescope: str = ''  # Fits Header name of TELESCOP
    instrument: str = ''  # Fits Header name of Instrument (can be partial)
    origin: str = ''  # Fits Header value of ORIGIN (if relevant)
    unique_headers: Optional[Dict[str, Any]] = None  # Unique key value pairs from header for identifying instrument.

    def __init__(self, image: FlowsImage = None, header: fits.header.Header = None):
        self.image = image
        self.hdr = header

    def get_site(self) -> Dict[str, Any]:
        if self.siteid is not None:
            return api.get_site(self.siteid)

    def get_exptime(self) -> Union[float, int, str]:
        exptime = self.image.header.get('EXPTIME', None)
        if exptime is None:
            raise ValueError("Image exposure time could not be extracted")
        return exptime

    def get_obstime(self) -> Time:
        """Default for JD, jd, utc."""
        return Time(self.image.header['JD'], format='jd', scale='utc', location=self.image.site['EarthLocation'])

    def get_photfilter(self):
        return self.image.header['FILTER']

    def set_instrument_defaults(self):
        """
        Set default values for instrument.
        """
        self.image.instrument_defaults = InstrumentDefaults()

    def _get_clean_image(self):
        self.image.peakmax = self.peakmax
        self.image.site = self.get_site()
        self.image.exptime = self.get_exptime()
        self.image.obstime = self.get_obstime()
        self.image.photfilter = self.get_photfilter()
        self.image.create_masked_image()

    def process_image(self, image: FlowsImage = None) -> FlowsImage:
        """Process existing or new image."""
        if image is not None:
            self.image = image
        if self.image is None:
            raise AttributeError('No FlowsImage to be processed. Self.image was None')

        self._get_clean_image()
        self.set_instrument_defaults()
        return self.image

    @classmethod
    def identifier(cls, telescope: str, origin: str, instrument: str, hdr: fits.header.Header) -> bool:
        """Unique identifier"""
        unique_conds = all([hdr.get(key) == cls.unique_headers.get(key) for key in cls.unique_headers.keys()] if cls.unique_headers is not None else [True])

        return all([cls.telescope in telescope if cls.telescope != '' else True,
                    cls.origin == origin if cls.origin != '' else True,
                    cls.instrument in instrument if cls.instrument != '' else True,
                    unique_conds])

    @staticmethod
    def get_ext(hdul: fits.HDUList, target_coords: coords.SkyCoord = None) -> int:
        """Instruments which need a special treatment to find the image extension
        should overwrite this."""
        return 0

    @staticmethod
    def get_mask(hdul: fits.HDUList) -> Optional[Any]:
        """Instruments which need a special treatment to find the mask should overwrite this."""
        return None


class LCOGT(Instrument):
    siteid = None  # Can be between 1, 3, 4, 6, 17, 19. @TODO: Refactor to own classes.
    peakmax: int = 60000
    origin = 'LCOGT'

    def get_site(self):
        nonesite = {'siteid': None}
        if self.image is None:
            return nonesite
        sites = api.sites.get_all_sites()
        site_keywords = {s['site_keyword']: s for s in sites}
        site = site_keywords.get(self.image.header['SITE'], nonesite)
        return site

    def get_obstime(self):
        observatory = coords.EarthLocation.from_geodetic(lat=self.image.header['LATITUDE'],
                                                         lon=self.image.header['LONGITUD'],
                                                         height=self.image.header['HEIGHT'])
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc', location=observatory)
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        photfilter = {'zs': 'zp'}.get(self.image.header['FILTER'], self.image.header['FILTER'])
        return photfilter

    @staticmethod
    def get_mask(hdul: fits.HDUList):
        if 'BPM' in hdul:
            return np.asarray(hdul['BPM'].data, dtype='bool')

        logger.warning('LCOGT image does not contain bad pixel map. Not applying mask.')
        return None


def verify_coordinates(target_coords: Union[coords.SkyCoord, Tuple]) -> Optional[coords.SkyCoord]:
    if target_coords is None:
        return None
    if isinstance(target_coords, coords.SkyCoord):
        return target_coords
    elif len(target_coords) == 2:
        return coords.SkyCoord(ra=target_coords[0] * u.deg, dec=target_coords[1] * u.deg, frame='icrs')
    return None


class HAWKI(Instrument):
    siteid = 2  # Hard-coded the siteid for ESO Paranal, VLT, UT4
    telescope = 'ESO-VLT-U4'  # Fits Header name of TELESCOP
    instrument = 'HAWKI'  # Fits Header name of Instrument (can be partial)
    origin = 'ESO-PARANAL'  # Fits Header value of ORIGIN (if relevant)
    unique_headers = {'PRODCATG': 'SCIENCE.MEFIMAGE'}

    def __init__(self, image: FlowsImage = None):
        super().__init__(image)
        if self.image is not None:
            self.get_obtype()

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_obtype(self):
        ob_type = self.image.header["HIERARCH ESO OCS DET1 IMGNAME"].split('_')[-1]
        if "Auto" in ob_type:
            self.image.ob_type = 'Autojitter'
        elif "Fixed" in ob_type:
            self.image.ob_type = 'FixedOffset'
        else:
            raise RuntimeError("Image OB Type not AutoJitter or FixedOffset")

    @staticmethod
    def get_ext(hdul: fits.HDUList, target_coords: coords.SkyCoord = None,
                fallback_extension: int = None) -> int:
        target_coord = verify_coordinates(target_coords)
        if target_coord is None:
            raise ValueError("TARGET_COORD is needed for HAWKI images to find the correct extension")

        # For HAWKI multi-extension images we search the extensions for which one contains
        # the target, Create Image from that extension.
        target_radec = [[target_coord.icrs.ra.deg, target_coord.icrs.dec.deg]]

        for k in range(1, 5):
            w = WCS(header=hdul[k].header, relax=True)
            s = [hdul[k].header['NAXIS2'], hdul[k].header['NAXIS1']]
            pix = w.all_world2pix(target_radec, 0).flatten()
            if -0.5 <= pix[0] <= s[1] - 0.5 and -0.5 <= pix[1] <= s[0] - 0.5:
                return k
        if fallback_extension is not None:
            return fallback_extension
        else:
            raise RuntimeError(f"Could not find image extension that target is on!")



class ALFOSC(Instrument):
    # Obtained from http://www.not.iac.es/instruments/detectors/CCD14/LED-linearity/20181026-200-1x1.pdf
    peakmax = 80000  # For ALFOSC D, 1x1, 200; the standard for SNe.
    siteid = 5
    telescope = "NOT"
    instrument = "ALFOSC"
    unique_headers = {"OBS_MODE": 'imaging'}

    def get_obstime(self):
        return Time(self.image.header['DATE-AVG'], format='isot', scale='utc',
                    location=self.image.site['EarthLocation'])

    def get_photfilter(self):
        # Sometimes data from NOT does not have the FILTER keyword,
        # in which case we have to try to figure out which filter
        # was used based on some of the other headers:
        if 'FILTER' in self.image.header:
            photfilter = {'B Bes': 'B', 'V Bes': 'V', 'R Bes': 'R', 'g SDSS': 'gp', 'r SDSS': 'rp', 'i SDSS': 'ip',
                          'i int': 'ip',  # Interference filter
                          'u SDSS': 'up', 'z SDSS': 'zp'}.get(self.image.header['FILTER'].replace('_', ' '),
                                                              self.image.header['FILTER'])
        else:
            filters_used = []
            for check_headers in ('ALFLTNM', 'FAFLTNM', 'FBFLTNM'):
                isopen = self.image.header.get(check_headers).strip().lower() != 'open'
                if self.image.header.get(check_headers) and isopen:
                    filters_used.append(self.image.header.get(check_headers).strip())
            if len(filters_used) == 1:
                photfilter = {'B_Bes 440_100': 'B', 'V_Bes 530_80': 'V', 'R_Bes 650_130': 'R', "g'_SDSS 480_145": 'gp',
                              "r'_SDSS 618_148": 'rp', "i'_SDSS 771_171": 'ip', 'i_int 797_157': 'ip',
                              # Interference filter
                              "z'_SDSS 832_LP": 'zp'}.get(filters_used[0].replace('  ', ' '), filters_used[0])
            else:
                raise RuntimeError("Could not determine filter used.")

        return photfilter


class NOTCAM(Instrument):
    siteid = 5
    telescope = "NOT"
    instrument = "NOTCAM"
    unique_headers = {"OBS_MODE": 'imaging'}

    def get_obstime(self):
        return Time(self.image.header['DATE-AVG'], format='isot', scale='utc',
                    location=self.image.site['EarthLocation'])

    def get_photfilter(self):
        # Does NOTCAM data sometimes contain a FILTER header?
        # if not we have to try to figure out which filter
        # was used based on some of the other headers:
        if 'FILTER' in self.image.header:
            raise RuntimeError("NOTCAM: Filter keyword defined")
        filters_used = []
        for check_headers in ('NCFLTNM1', 'NCFLTNM2'):
            isopen = self.image.header.get(check_headers).strip().lower() != 'open'
            if self.image.header.get(check_headers) and isopen:
                filters_used.append(self.image.header.get(check_headers).strip())
        if len(filters_used) == 1:
            photfilter = {'Ks': 'K'}.get(filters_used[0], filters_used[0])
        else:
            raise RuntimeError("Could not determine filter used.")
        return photfilter


class PS1(Instrument):
    siteid = 6
    unique_headers = {'FPA.TELESCOPE': 'PS1', 'FPA.INSTRUMENT': 'GPC1'}

    def get_obstime(self):
        return Time(self.image.header['MJD-OBS'], format='mjd', scale='utc', location=self.image.site['EarthLocation'])

    def get_photfilter(self):
        photfilter = {'g.00000': 'gp', 'r.00000': 'rp', 'i.00000': 'ip', 'z.00000': 'zp'}.get(
            self.image.header['FPA.FILTER'], self.image.header['FPA.FILTER'])
        return photfilter


class Liverpool(Instrument):
    siteid = 8
    telescope = 'Liverpool Telescope'

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        photfilter = {'Bessel-B': 'B', 'Bessell-B': 'B', 'Bessel-V': 'V', 'Bessell-V': 'V', 'SDSS-U': 'up',
                      'SDSS-G': 'gp', 'SDSS-R': 'rp', 'SDSS-I': 'ip', 'SDSS-Z': 'zp'}.get(self.image.header['FILTER1'],
                                                                                          self.image.header['FILTER1'])
        return photfilter


class Omega2000(Instrument):
    siteid = 9
    telescope = 'CA 3.5m'
    instrument = 'Omega2000'

    def get_obstime(self):
        obstime = Time(self.image.header['MJD-OBS'], format='mjd', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second
        return obstime


class Swope(Instrument):
    siteid = 10
    telescope = "SWO"

    def get_photfilter(self):
        photfilter = {'u': 'up', 'g': 'gp', 'r': 'rp', 'i': 'ip', }.get(self.image.header['FILTER'],
                                                                        self.image.header['FILTER'])
        return photfilter

    @classmethod
    def identifier(cls, telescope: str, origin: str, instrument: str, hdr: fits.header.Header) -> bool:
        """Unique identifier"""
        return telescope.upper().startswith(cls.telescope) and hdr.get('SITENAME') == 'LCO'


class Swope_newheader(Swope):

    def get_obstime(self):
        obstime = Time(self.image.header['MJD-OBS'], format='mjd', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second
        return obstime

    @classmethod
    def identifier(cls, telescope: str, origin: str, instrument: str, hdr: fits.header.Header) -> bool:
        """Unique identifier"""
        return telescope.upper().startswith('SWO') and origin == 'ziggy'


class Dupont(Instrument):
    siteid = 14
    telescope = 'DUP'
    instrument = 'Direct/SITe2K-1'
    unique_headers = {'SITENAME': 'LCO'}

    def get_photfilter(self):
        photfilter = {'u': 'up', 'g': 'gp', 'r': 'rp', 'i': 'ip', }.get(self.image.header['FILTER'],
                                                                        self.image.header['FILTER'])
        return photfilter


class RetroCam(Instrument):
    siteid = 16
    telescope = 'DUP'
    instrument = 'RetroCam'

    def get_photfilter(self):
        photfilter = {'Yc': 'Y', 'Hc': 'H', 'Jo': 'J', }.get(self.image.header['FILTER'], self.image.header['FILTER'])
        return photfilter


class Baade(Instrument):
    siteid = 11
    telescope = 'Baade'
    instrument = 'FourStar'
    unique_headers = {'SITENAME': 'LCO'}

    def get_exptime(self):
        exptime = super().get_exptime()
        exptime *= int(self.image.header['NCOMBINE'])  # EXPTIME is only for a single exposure
        return exptime

    def get_photfilter(self):
        photfilter = {'Ks': 'K', 'J1': 'Y', }.get(self.image.header['FILTER'], self.image.header['FILTER'])
        return photfilter


class Sofi(Instrument):
    siteid = 12
    instrument = 'SOFI'

    def get_obstime(self):
        if 'TMID' in self.image.header:
            obstime = Time(self.image.header['TMID'], format='mjd', scale='utc',
                           location=self.image.site['EarthLocation'])
        else:
            obstime = Time(self.image.header['MJD-OBS'], format='mjd', scale='utc',
                           location=self.image.site['EarthLocation'])
            obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        hdr = self.image.header
        photfilter_translate = {'Ks': 'K'}
        if 'FILTER' in hdr:
            photfilter = photfilter_translate.get(hdr['FILTER'], hdr['FILTER'])
        else:
            filters_used = []
            for check_headers in ('ESO INS FILT1 ID', 'ESO INS FILT2 ID'):
                if hdr.get(check_headers) and hdr.get(check_headers).strip().lower() != 'open':
                    filters_used.append(hdr.get(check_headers).strip())
            if len(filters_used) == 1:
                photfilter = photfilter_translate.get(filters_used[0], filters_used[0])
            else:
                raise RuntimeError("Could not determine filter used.")
        return photfilter

    @classmethod
    def identifier(cls, telescope, origin, instrument, hdr):
        return instrument == cls.instrument and telescope in ('ESO-NTT', 'other')


class EFOSC(Instrument):
    siteid = 15
    telescope = 'ESO-NTT'
    instrument = 'EFOSC'

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        hdr = self.image.header
        photfilter = {'g782': 'gp', 'r784': 'rp', 'i705': 'ip', 'B639': 'B', 'V641': 'V'}.get(hdr['FILTER'],
                                                                                              hdr['FILTER'])
        return photfilter


class AstroNIRCam(Instrument):
    siteid = 13
    telescope = 'SAI-2.5'
    instrument = 'ASTRONIRCAM'

    def get_exptime(self):
        exptime = self.image.header.get('FULL_EXP', None)
        if exptime is not None:
            return exptime
        return super().get_exptime()

    def get_obstime(self):
        hdr = self.image.header
        if 'MIDPOINT' in hdr:
            obstime = Time(hdr['MIDPOINT'], format='isot', scale='utc', location=self.image.site['EarthLocation'])
        else:
            obstime = Time(hdr['MJD-AVG'], format='mjd', scale='utc', location=self.image.site['EarthLocation'])
        return obstime

    def get_photfilter(self):
        hdr = self.image.header
        photfilter = {'H_Open': 'H', 'K_Open': 'K', }.get(hdr['FILTER'], hdr['FILTER'])
        return photfilter


class OmegaCam(Instrument):
    siteid = 18  # Hard-coded the siteid for ESO VLT Survey telescope
    instrument = 'OMEGACAM'

    def get_obstime(self):
        obstime = Time(self.image.header['MJD-OBS'], format='mjd', scale='utc',
                       location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        hdr = self.image.header
        photfilter = {'i_SDSS': 'ip'}.get(hdr['ESO INS FILT1 NAME'], hdr['ESO INS FILT1 NAME'])
        return photfilter


class AndiCam(Instrument):
    siteid = 20  # Hard-coded the siteid for ANDICAM at Cerro Tololo Interamerican Observatory (CTIO)
    instrument = 'ANDICAM-CCD'
    unique_headers = {'OBSERVAT': 'CTIO'}

    def get_obstime(self):
        obstime = super().get_obstime()
        obstime += 0.5 * self.image.exptime * u.second
        return obstime

    def get_photfilter(self):
        return self.image.header['CCDFLTID']


class PairTel(Instrument):
    siteid = 21
    telescope = "1.3m PAIRITEL"
    instrument = "2MASS Survey cam"

    def get_obstime(self):
        hdr = self.image.header
        time_start = Time(hdr['STRT_CPU'], format='iso', scale='utc', location=self.image.site['EarthLocation'])
        time_stop = Time(hdr['STOP_CPU'], format='iso', scale='utc', location=self.image.site['EarthLocation'])
        obstime = time_start + 0.5 * (time_stop - time_start)
        return obstime

    def get_photfilter(self):
        hdr = self.image.header
        photfilter = {'j': 'J', 'h': 'H', 'k': 'K', }.get(hdr['FILTER'], hdr['FILTER'])
        return photfilter


class TJO_MEIA2(Instrument):
    siteid = 22
    telescope = 'TJO'
    instrument = 'MEIA2'


    def get_obstime(self):
        obstime = super().get_obstime()
        obstime += 0.5 * self.image.exptime * u.second
        return obstime


class TJO_MEIA3(Instrument):
    siteid = 22
    telescope = 'TJO'
    instrument = 'MEIA3'

    def get_obstime(self):
        obstime = super().get_obstime()
        obstime += 0.5 * self.image.exptime * u.second
        return obstime


class RATIR(Instrument):
    siteid = 23
    telescope = "OAN/SPM Harold L. Johnson 1.5-meter"

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                        location=self.image.site['EarthLocation'])
        return obstime

    def get_photfilter(self):
        ratir_filt = self.image.header['FILTER']
        if ratir_filt in ['Z', 'r', 'i']:
            return {'Z': 'zp', 'r': 'rp', 'i': 'ip'}.get(ratir_filt)
        return ratir_filt


class AFOSC(Instrument):
    siteid = 25
    peakmax = 50_000
    telescope: '1.82m Reflector'  # Fits Header name of TELESCOP
    instrument: 'AFOSC'  # Fits Header name of Instrument (can be partial)

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                        location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        filt = self.image.header['FILTER']

        if "sloan" in filt.lower():
            return filt[0]+'p'  # Return gp,rp,ip,zp for g-sloan, etc.
        elif filt in FILTERS.keys():
            return filt
        elif filt+'p' in FILTERS.keys():
            return filt+'p'
        elif filt[0] in FILTERS.keys():
            return filt[0]

        raise ValueError(f"Could not find filter {filt} in {[f for f in FILTERS.keys()]}")


class Schmidt(Instrument):
    siteid = 26
    peakmax = 56_000
    telescope: '67/91 Schmidt Telescope'  # Fits Header name of TELESCOP
    instrument: 'Moravian G4-16000LC'  # Fits Header name of Instrument (can be partial)
    origin: ''  # Fits Header value of ORIGIN (if relevant)
    unique_headers = {
        'SITELAT': 45.8494444
    }  # Unique key value pairs from header for identifying instrument.

    def get_obstime(self):
        obstime = Time(self.image.header['DATE-OBS'], format='isot', scale='utc',
                        location=self.image.site['EarthLocation'])
        obstime += 0.5 * self.image.exptime * u.second  # Make time centre of exposure
        return obstime

    def get_photfilter(self):
        filt = self.image.header['FILTER']

        if "sloan" in filt.lower():
            return filt[0]+'p'  # Return gp,rp,ip,zp for g-sloan, etc.
        elif filt in FILTERS.keys():
            return filt
        elif filt+'p' in FILTERS.keys():
            return filt+'p'

        raise ValueError(f"Could not find filter {filt} in {[f for f in FILTERS.keys()]}")



instruments = {'LCOGT': LCOGT, 'HAWKI': HAWKI, 'ALFOSC': ALFOSC, 'NOTCAM': NOTCAM, 'PS1': PS1, 'Liverpool': Liverpool,
               'Omega2000': Omega2000, 'Swope': Swope, 'Swope_newheader':Swope_newheader, 'Dupont': Dupont, 'Retrocam':
                   RetroCam, 'Baade': Baade,
               'Sofi': Sofi, 'EFOSC': EFOSC, 'AstroNIRCam': AstroNIRCam, 'OmegaCam': OmegaCam, 'AndiCam': AndiCam,
               'PairTel': PairTel, 'TJO_Meia2': TJO_MEIA2, 'TJO_Meia3': TJO_MEIA3, 'RATIR': RATIR, "Schmidt": Schmidt, "AFOSC": AFOSC}


def correct_barycentric(obstime: Time, target_coord: coords.SkyCoord) -> Time:
    """
    BARYCENTRIC CORRECTION OF TIME

    Parameters:
        obstime (astropy.time.Time): Midpoint observed image time.
        target_coord (astropy.coords.SkyCoord): Coordinates of target in image.

    Returns:
        obstime (astropy.time.Time): Time corrected to barycenter with jpl ephemeris.
    """
    ltt_bary = obstime.light_travel_time(target_coord, ephemeris='jpl')
    return obstime.tdb + ltt_bary


def load_image(filename: str, target_coord: typing.Union[coords.SkyCoord, typing.Tuple[float, float]] = None):
    """
    Load FITS image using FlowsImage class and Instrument Classes.

    Parameters:
        filename (str): Path to FITS file to be loaded.
        target_coord (:class:`astropy.coordinates.SkyCoord`): Coordinates of target.
            Only used for HAWKI images to determine which image extension to load,
            for all other images it is ignored.

    Returns:
        FlowsImage: instance of FlowsImage with values populated based on instrument.

    """
    ext = 0  # Default extension is  0, individual instruments may override this.
    # Read fits image, Structural Pattern Match to specific instrument.
    with fits.open(filename, mode='readonly') as hdul:
        hdr = hdul[ext].header
        origin = hdr.get('ORIGIN', '')
        telescope = hdr.get('TELESCOP', '')
        instrument = hdr.get('INSTRUME', '')

        for name, inst_cls in instruments.items():
            if inst_cls.identifier(telescope, origin, instrument, hdr):
                ext = inst_cls.get_ext(hdul, target_coord)
                mask = inst_cls.get_mask(hdul)
                # Default = None is to only mask all non-finite values, override here is additive.

                image = FlowsImage(image=np.asarray(hdul[ext].data, dtype='float64'),
                                   header=hdr, mask=mask)
                current_instrument = inst_cls(image)
                clean_image = current_instrument.process_image()
                if target_coord is not None:
                    clean_image.obstime = correct_barycentric(clean_image.obstime, target_coord)
                return clean_image

        raise RuntimeError("Could not determine origin of image")

