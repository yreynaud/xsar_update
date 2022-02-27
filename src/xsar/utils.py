"""miscellaneous functions"""

from functools import wraps
import time
import os
import numpy as np
import logging
from scipy.interpolate import griddata
import xarray as xr
import dask
from dask.distributed import get_client, secede, rejoin
from functools import wraps, partial
import rasterio
import shutil
import glob
import yaml
import re

logger = logging.getLogger('xsar.utils')
logger.addHandler(logging.NullHandler())

mem_monitor = True

try:
    from psutil import Process
except ImportError:
    logger.warning("psutil module not found. Disabling memory monitor")
    mem_monitor = False


class bind(partial):
    """
    An improved version of partial which accepts Ellipsis (...) as a placeholder
    https://stackoverflow.com/a/66274908
    """

    def __call__(self, *args, **keywords):
        keywords = {**self.keywords, **keywords}
        iargs = iter(args)
        args = (next(iargs) if arg is ... else arg for arg in self.args)
        return self.func(*args, *iargs, **keywords)


class class_or_instancemethod(classmethod):
    # see https://stackoverflow.com/a/28238047/5988771
    def __get__(self, instance, type_):
        descr_get = super().__get__ if instance is None else self.__func__.__get__
        return descr_get(instance, type_)


def timing(f):
    """provide a @timing decorator for functions, that log time spent in it"""

    @wraps(f)
    def wrapper(*args, **kwargs):
        mem_str = ''
        process = None
        if mem_monitor:
            process = Process(os.getpid())
            startrss = process.memory_info().rss
        starttime = time.time()
        result = f(*args, **kwargs)
        endtime = time.time()
        if mem_monitor:
            endrss = process.memory_info().rss
            mem_str = 'mem: %+.1fMb' % ((endrss - startrss) / (1024 ** 2))
        logger.debug(
            'timing %s : %.2fs. %s' % (f.__name__, endtime - starttime, mem_str))
        return result

    return wrapper


def to_lon180(lon):
    """

    Parameters
    ----------
    lon: array_like of float
        longitudes in [0, 360] range

    Returns
    -------
    array_like
        longitude in [-180, 180] range

    """
    change = lon > 180
    lon[change] = lon[change] - 360
    return lon


def haversine(lon1, lat1, lon2, lat2):
    """
    Compute distance in meters, and bearing in degrees from point1 to point2, assuming spherical earth.

    Parameters
    ----------
    lon1: float
    lat1: float
    lon2: float
    lat2: float

    Returns
    -------
    tuple(float, float)
        distance in meters, and bearing in degrees

    """
    # convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])

    # haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    c = 2 * np.arcsin(np.sqrt(a))
    r = 6371000  # Radius of earth in meters.
    bearing = np.arctan2(np.sin(lon2 - lon1) * np.cos(lat2),
                         np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(lon2 - lon1))
    return c * r, np.rad2deg(bearing)


def minigrid(x, y, z, method='linear', dims=['x', 'y']):
    """

    Parameters
    ----------
    x: 1D array_like
        x coordinates

    y: 1D array_like
        y coodinate

    z: 1D array_like
        value at [x, y] coordinates

    method: str
        default to 'linear'. passed to `scipy.interpolate.griddata`

    dims: list of str
        dimensions names for returned dataarray. default to `['x', 'y']`

    Returns
    -------
    xarray.DataArray
        2D grid of `z` interpolated values, with 1D coordinates `x` and `y`.
    """
    x_u = np.unique(np.sort(x))
    y_u = np.unique(np.sort(y))
    xx, yy = np.meshgrid(x_u, y_u, indexing='ij')
    ngrid = griddata((x, y), z, (xx, yy), method=method)
    return xr.DataArray(ngrid, dims=dims, coords={dims[0]: x_u, dims[1]: y_u})


def map_blocks_coords(da, func, withburst=False, func_kwargs={}, **kwargs):
    """
    like `dask.map_blocks`, but `func` parameters are dimensions coordinates belonging to the block.

    Parameters
    ----------
    da: xarray.DataArray
        template (meta) of the output dataarray, with dask chunks, dimensions, coordinates and dtype
    func: function or future
        function that take gridded `numpy.array` atrack and xtrack, and return a `numpy.array`.
        (see `_evaluate_from_coords`)
    kwargs: dict
        passed to dask.array.map_blocks

    Returns
    -------
    xarray.DataArray
        dataarray with same coords/dims as self from `func(*dims)`.
    """

    def _evaluate_from_coords(block, f, coords, block_info=None, dtype=None):
        """
        evaluate 'f(x_coords,y_coords,...)' with x_coords, y_coords, ... extracted from dims

        Parameters
        ----------
        coords: iterable of numpy.array
            coordinates for each dimension block
        block: numpy.array
            the current block in the dataarray
        f: function
            function to evaluate with 'func(atracks_grid, xtracks_grid)'
        block_info: dict
            provided by 'xarray.DataArray.map_blocks', and used to get block location.

        Returns
        -------
        numpy.array
            result from 'f(*coords_sel)', where coords_sel are dimension coordinates for each dimensions

        Notes
        -----
        block values are not used.
        Unless manualy providing block_info,
        this function should be called from 'xarray.DataArray.map_blocks' with coords preset with functools.partial
        """

        # get loc ((dim_0_start,dim_0_stop),(dim_1_start,dim_1_stop),...)
        try:
            loc = block_info[None]['array-location']
        except TypeError:
            # map_blocks is feeding us some dummy block data to check output type and shape
            # so we juste generate dummy coords to be able to call f
            # (Note : dummy coords are 0 sized if dummy block is empty)
            loc = tuple(zip((0,) * len(block.shape), block.shape))

        # use loc to get corresponding coordinates
        coords_sel = tuple(c[loc[i][0]:loc[i][1]] for i, c in enumerate(coords))

        result = f(*coords_sel, **func_kwargs)

        if dtype is not None:
            result = result.astype(dtype)

        return result
    def _evaluate_from_azimuth_time(block, f, coords,azimuthtime, block_info=None, dtype=None):
        # get loc ((dim_0_start,dim_0_stop),(dim_1_start,dim_1_stop),...)
        try:
            loc = block_info[None]['array-location']
        except TypeError:
            # map_blocks is feeding us some dummy block data to check output type and shape
            # so we juste generate dummy coords to be able to call f
            # (Note : dummy coords are 0 sized if dummy block is empty)
            loc = tuple(zip((0,) * len(block.shape), block.shape))
        logger.debug('loc %s',loc)
        azaz = azimuthtime[loc[0][0]:loc[0][1]].astype(float) # cast float before interpolation
        logger.debug('azaz : %s %s',azaz,azaz.shape)
        coords_sel = []
        for i, c in enumerate(coords):
            tmptmp = c[loc[i][0]:loc[i][1]]
            coords_sel.append(tmptmp)

        range_coords = coords_sel[1]
        logger.debug('range coords %s %s',range_coords,range_coords.shape)
        logger.debug('block %s %s %s',block,type(block),block.shape)
        #logger.debug('func kwargs %s',**func_kwargs)
        result = f(azaz[:, np.newaxis],range_coords[np.newaxis,:],**func_kwargs)
        if dtype is not None:
            result = result.astype(dtype)
        return result
    if withburst is True:
        # TOPS SLC

        coords = {c: da[c].values for c in da.dims}
        coords_4_interpolation = {'xint': da['xint'].values.astype(float), 'xtrack': da['xtrack'].values}
    else:
        coords = {c: da[c].values for c in da.dims}
        coords_4_interpolation = coords
    if 'name' not in kwargs:
        kwargs['name'] = dask.utils.funcname(func)

    meta = da.data
    dtype = meta.dtype

    if withburst:
        #TOPS SLC
        from_coords = bind(_evaluate_from_azimuth_time, ..., ..., coords_4_interpolation.values(),azimuthtime=coords_4_interpolation['xint'], dtype=dtype)
    else:
        from_coords = bind(_evaluate_from_coords, ..., ..., coords_4_interpolation.values(), dtype=dtype)

    daskarr = meta.map_blocks(from_coords, func, meta=meta, **kwargs)
    dataarr = xr.DataArray(daskarr,
                           dims=da.dims,
                           coords=coords
                           )
    return dataarr


def bbox_coords(xs, ys, pad='extends'):
    """
    [(xs[0]-padx, ys[0]-pady), (xs[0]-padx, ys[-1]+pady), (xs[-1]+padx, ys[-1]+pady), (xs[-1]+padx, ys[0]-pady)]
    where padx and pady are xs and ys spacing/2
    """
    bbox_norm = [(0, 0), (0, -1), (-1, -1), (-1, 0)]
    if pad == 'extends':
        xdiff, ydiff = [np.unique(np.diff(d))[0] for d in (xs, ys)]
        xpad = (-xdiff / 2, xdiff / 2)
        ypad = (-ydiff / 2, ydiff / 2)
    elif pad is None:
        xpad = (0, 0)
        ypad = (0, 0)
    else:
        xpad = (-pad[0], pad[0])
        ypad = (-pad[1], pad[1])
    # use apad and xpad to get surrounding box
    bbox_ext = [
        (
            xs[x] + xpad[x],
            ys[y] + ypad[y]
        ) for x, y in bbox_norm
    ]
    return bbox_ext


def compress_safe(safe_path_in, safe_path_out, smooth=0, rasterio_kwargs={'compress': 'zstd'}):
    """

    Parameters
    ----------
    safe_path_in: str
        input SAFE path
    safe_path_out: str
        output SAFE path (be warned to keep good nomenclature)
    rasterio_kwargs: dict
        passed to rasterio.open

    Returns
    -------
    str
        wrotten output path

    """

    safe_path_out_tmp = safe_path_out + '.tmp'
    if os.path.exists(safe_path_out):
        raise FileExistsError("%s already exists" % safe_path_out)
    try:
        shutil.rmtree(safe_path_out_tmp)
    except:
        pass
    os.mkdir(safe_path_out_tmp)

    shutil.copytree(safe_path_in + "/annotation", safe_path_out_tmp + "/annotation")
    shutil.copyfile(safe_path_in + "/manifest.safe", safe_path_out_tmp + "/manifest.safe")

    os.mkdir(safe_path_out_tmp + "/measurement")
    for tiff_file in glob.glob(os.path.join(safe_path_in, 'measurement', '*.tiff')):
        src = rasterio.open(tiff_file)
        open_kwargs = src.profile
        open_kwargs.update(rasterio_kwargs)
        gcps, crs = src.gcps
        open_kwargs['gcps'] = gcps
        open_kwargs['crs'] = crs
        if smooth > 1:
            reduced = xr.DataArray(
                src.read(
                    1, out_shape=(src.height // smooth, src.width // smooth),
                    resampling=rasterio.enums.Resampling.rms))
            mean = reduced.mean().item()
            if not isinstance(mean, complex) and mean < 1:
                raise RuntimeError('rasterio returned empty band. Try to use smallest smooth size')
            reduced = reduced.assign_coords(
                dim_0=reduced.dim_0 * smooth + smooth / 2,
                dim_1=reduced.dim_1 * smooth + smooth / 2)
            band = reduced.interp(
                dim_0=np.arange(src.height),
                dim_1=np.arange(src.width),
                method='nearest').values.astype(src.dtypes[0])
        else:
            band = src.read(1)

        with rasterio.open(
                safe_path_out_tmp + "/measurement/" + os.path.basename(tiff_file),
                'w',
                **open_kwargs
        ) as dst:
            dst.write(band, 1)

    os.rename(safe_path_out_tmp, safe_path_out)

    return safe_path_out


class BlockingActorProxy():
    # http://distributed.dask.org/en/stable/actors.html
    # like dask Actor, but no need to do .result() on methods
    # so the resulting instance is usable like the proxied instance
    def __init__(self, cls, *args, actor=True, **kwargs):

        # the class to be proxied  (ie Sentinel1Meta)
        self._cls = cls
        self._actor = None

        # save for unpickling
        self._args = args
        self._kwargs = kwargs

        self._dask_client = None
        if actor is True:
            try:
                self._dask_client = get_client()
            except ValueError:
                logger.info('BlockingActorProxy: Transparent proxy for %s' % self._cls.__name__)
        elif isinstance(actor, dask.distributed.actor.Actor):
            logger.debug('BlockingActorProxy: Reusing existing actor')
            self._actor = actor

        if self._dask_client is not None:
            logger.debug('submit new actor')
            self._actor_future = self._dask_client.submit(self._cls, *args, **kwargs, actors=True)
            self._actor = self._actor_future.result()
        elif self._actor is None:
            # transparent proxy: no future
            self._actor = self._cls(*args, **kwargs)

    def __repr__(self):
        return f"<BlockingActorProxy: {self._cls.__name__}>"

    def __dir__(self):
        o = set(dir(type(self)))
        o.update(attr for attr in dir(self._cls) if not attr.startswith("_"))
        return sorted(o)

    def __getattr__(self, key):
        attr = getattr(self._actor, key)
        if not callable(attr):
            return attr
        else:
            @wraps(attr)
            def func(*args, **kwargs):
                res = attr(*args, **kwargs)
                if isinstance(res, dask.distributed.ActorFuture):
                    return res.result()
                else:
                    # transparent proxy
                    return res

            return func

    def __reduce__(self):
        # make self serializable with pickle
        # https://docs.python.org/3/library/pickle.html#object.__reduce__
        kwargs = self._kwargs
        kwargs['actor'] = self._actor
        return partial(BlockingActorProxy, **kwargs), (self._cls, *self._args)


def merge_yaml(yaml_strings_list,section=None):
    # merge a list of yaml strings in one string

    dict_like = yaml.safe_load(
            '\n'.join(yaml_strings_list)
        )
    if section is not None:
        dict_like = {section: dict_like}

    return yaml.safe_dump(dict_like)

def get_glob(strlist):
    # from list of str, replace diff by '?'
    def _get_glob(st):
        stglob = ''.join(
            [
                '?' if len(charlist) > 1 else charlist[0]
                for charlist in [list(set(charset)) for charset in zip(*st)]
            ]
        )
        return re.sub(r'\?+', '*', stglob)

    strglob = _get_glob(strlist)
    if strglob.endswith('*'):
        strglob += _get_glob(s[::-1] for s in strlist)[::-1]
        strglob = strglob.replace('**', '*')

    return strglob
