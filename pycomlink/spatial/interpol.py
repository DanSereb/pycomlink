#----------------------------------------------------------------------------
# Name:         
# Purpose:      
#
# Authors:      
#
# Created:      
# Copyright:    (c) Christian Chwala 2016
# Licence:      The MIT License
#----------------------------------------------------------------------------

from __future__ import division

import numpy as np
import pandas as pd
import xarray as xr
from scipy.spatial import cKDTree as KDTree

from tqdm import tqdm
from ok import kriging

import geopandas
import shapely as sh

class Interpolator(object):
    def __init__(self,
                 cml_list,
                 channel_name='channel_1',
                 xgrid=None,
                 ygrid=None,
                 resolution=None,
                 resample_time='H',
                 resample_func='mean',
                 resample_label='right',
                 apply_factor=1,
                 variable='R'):
        self.cml_list = cml_list
        self.variable = variable

        self.gridded_data = None
        self.grid_points_covered_by_cmls = None

        if (xgrid is None) or (ygrid is None):
            lats = ([cml.metadata['site_a_latitude'] for cml in cml_list] +
                    [cml.metadata['site_b_latitude'] for cml in cml_list])
            lons = ([cml.metadata['site_a_longitude'] for cml in cml_list] +
                    [cml.metadata['site_b_longitude'] for cml in cml_list])

            if resolution is None:
                resolution = 0.01

            xcoords = np.arange(min(lons) - resolution,
                                max(lons) + resolution,
                                resolution)
            ycoords = np.arange(min(lats) - resolution,
                                max(lats) + resolution,
                                resolution)
            xgrid, ygrid = np.meshgrid(xcoords, ycoords)
            xi, yi = xgrid.flatten(), ygrid.flatten()

            #self.grid = np.vstack((xi, yi)).T
            self.xgrid = xgrid
            self.ygrid = ygrid
        else:
            self.xgrid = xgrid
            self.ygrid = ygrid

        # Resample time series of each CML
        self.df_cmls_R = pd.DataFrame()
        # TODO: Add options average results for several channels
        for cml in self.cml_list:
            self.df_cmls_R[cml.metadata['cml_id']] = (
                cml.channels[channel_name].data[self.variable]
                .resample(resample_time, label=resample_label)
                .apply(resample_func))
        self.df_cmls_R *= apply_factor

        # Extract lats and lons
        self.lons = np.array(
            [cml.get_center_lon_lat()[0] for cml in self.cml_list])
        self.lats = np.array(
            [cml.get_center_lon_lat()[1] for cml in self.cml_list])


    def calc_coverage_mask(self, max_dist_from_cml, add_to_gridded_data=True):
        #TODO: Add option to do this for each time step, based on the
        #      available CML in self.df_cml_R, i.e. exclusing those
        #      with NaN.

        # Build a polygon for the area "covered" by the CMLs
        # given a maximum distance from their individual paths
        cml_lines = []
        for cml in self.cml_list:
            cml_lines.append(
                sh.geometry.LineString([
                    [cml.metadata['site_a_longitude'],
                     cml.metadata['site_a_latitude']],
                    [cml.metadata['site_b_longitude'],
                     cml.metadata['site_b_latitude']]])
                .buffer(max_dist_from_cml, cap_style=1))

        cml_dil_union = sh.ops.cascaded_union(cml_lines)
        # Build a geopandas object for this polygon
        gdf_cml_area = geopandas.GeoDataFrame(
            geometry=geopandas.GeoSeries(cml_dil_union))

        # Generate a geopandas object for all grip points
        sh_grid_point_list = [sh.geometry.Point(xy) for xy
                              in zip(self.xgrid.flatten(),
                                     self.ygrid.flatten())]
        gdf_grid_points = geopandas.GeoDataFrame(
            geometry=sh_grid_point_list)

        # Find all grid points within the area covered by the CMLs
        points_in_cml_area = geopandas.sjoin(gdf_grid_points,
                                             gdf_cml_area,
                                             how='left')

        # Generate a Boolean grid with shape of xgrid (and ygrid)
        # indicating which grid points are within the area covered by CMLs
        grid_point_covered_by_cmls = (
            (~points_in_cml_area.index_right.isnull())
            .values.reshape(self.xgrid.shape))

        self.grid_points_covered_by_cmls = grid_point_covered_by_cmls

        if add_to_gridded_data and (self.gridded_data is not None):
            self.gridded_data['coverage_mask'] = (
                ['x', 'y'], grid_point_covered_by_cmls)

        return grid_point_covered_by_cmls

    def kriging(self, n_closest_points):
        # TODO: FIX Kriging. Results do not yet make sense...
        fields = []

        for t, row in self.df_cmls_R.iterrows():
            values = row.values
            i_not_nan = ~pd.isnull(values)
            interp_values = kriging(self.lons[i_not_nan],
                                    self.lats[i_not_nan],
                                    values,
                                    self.xgrid,
                                    self.ygrid,
                                    n_closest_points=n_closest_points)
            fields.append(interp_values)

        self.gridded_data = self._fields_to_dataset(fields)
        return self.gridded_data

    def idw(self, max_dist=None, power=2):
        fields = []

        for t, row in self.df_cmls_R.iterrows():
            values = row.values
            i_not_nan = ~pd.isnull(values)
            interp_values = idw(values[i_not_nan],
                                self.lons[i_not_nan],
                                self.lats[i_not_nan],
                                self.xgrid,
                                self.ygrid,
                                max_dist=None,
                                p=power)
            fields.append(interp_values)

        self.gridded_data = self._fields_to_dataset(fields)
        return self.gridded_data

    def idw_kdtree(self, nnear=10, p=2, eps=0.1, progress_bar=False,
                   t_start=None, t_stop=None):
        fields = []

        if t_start is None:
            t_start = self.df_cmls_R.index[0]
        if t_stop is None:
            t_stop = self.df_cmls_R.index[-1]

        if progress_bar:
            pbar = tqdm(total=len(self.df_cmls_R[t_start:t_stop].index))

        for t, row in self.df_cmls_R[t_start:t_stop].iterrows():
            values = row.values
            i_not_nan = ~pd.isnull(values)

            idw_tree = Invdisttree(np.array([self.lons[i_not_nan],
                                             self.lats[i_not_nan]]).T,
                                   values[i_not_nan],
                                   leafsize=nnear+2)
            interp_values = idw_tree(np.array([self.xgrid.flatten(),
                                               self.ygrid.flatten()]).T,
                                     nnear=nnear,
                                     p=p,
                                     eps=eps)
            interp_values = np.reshape(interp_values, self.xgrid.shape)
            fields.append(interp_values)

            if progress_bar:
                pbar.update(1)

        # Close progress bar
        if progress_bar:
            pbar.close()

        self.gridded_data = self._fields_to_dataset(fields, t_start, t_stop)
        return self.gridded_data

    def rbf(self):
        from scipy.interpolate import Rbf

        fields = []
        for t, row in self.df_cmls_R.iterrows():
            values = row.values
            i_not_nan = ~pd.isnull(values)

            rbf = Rbf(self.lons[i_not_nan],
                      self.lats[i_not_nan],
                      values[i_not_nan],
                      function='linear')
            interp_values = rbf(self.xgrid, self.ygrid)
            fields.append(interp_values)

        self.gridded_data = self._fields_to_dataset(fields)
        return self.gridded_data

    def _fields_to_dataset(self, fields, t_start=None, t_stop=None):
        if t_start is None:
            t_start = self.df_cmls_R.index[0]
        if t_stop is None:
            t_stop = self.df_cmls_R.index[-1]

        ds = xr.Dataset({self.variable: (['x', 'y', 'time'],
                                         np.moveaxis(np.array(fields),
                                                             0, -1))},
                        coords={'lon': (['x', 'y'], self.xgrid),
                                'lat': (['x', 'y'], self.ygrid),
                                'time': self.df_cmls_R[t_start:t_stop].index})
        return ds


def idw(z, x, y, xi, yi, max_dist=None, p=2):

    # Code adapted from
    # http://stackoverflow.com/questions/3104781/...
    # inverse-distance-weighted-idw-interpolation-with-python

    ny, nx = xi.shape

    dist = distance_matrix(x,y, xi.flatten(), yi.flatten())

    if max_dist is not None:
        dist[dist > max_dist] = 0

    # In IDW, weights are 1 / distance
    weights = 1.0 / dist**p

    # Make weights sum to one
    weights /= weights.sum(axis=0)

    # Multiply the weights for each interpolated point by all observed Z-values
    zi = np.dot(weights.T, z)

    zi = zi.reshape((ny,nx))

    return zi


def distance_matrix(x0, y0, x1, y1):
    # Code adapted from
    # http://stackoverflow.com/questions/3104781/...
    # inverse-distance-weighted-idw-interpolation-with-python

    obs = np.vstack((x0, y0)).T
    interp = np.vstack((x1, y1)).T

    # Make a distance matrix between pairwise observations
    # Note: from <http://stackoverflow.com/questions/1871536>
    # (Yay for ufuncs!)
    d0 = np.subtract.outer(obs[:,0], interp[:,0])
    d1 = np.subtract.outer(obs[:,1], interp[:,1])

    return np.hypot(d0, d1)


class Invdisttree(object):
    """ inverse-distance-weighted interpolation using KDTree:

    Taken from http://stackoverflow.com/questions/3104781/
    inverse-distance-weighted-idw-interpolation-with-python

    Licence: CC BY-NC-SA 3.0

    invdisttree = Invdisttree( X, z )  -- data points, values
    interpol = invdisttree( q, nnear=3, eps=0, p=1, weights=None, stat=0 )
        interpolates z from the 3 points nearest each query point q;
        For example, interpol[ a query point q ]
        finds the 3 data points nearest q, at distances d1 d2 d3
        and returns the IDW average of the values z1 z2 z3
            (z1/d1 + z2/d2 + z3/d3)
            / (1/d1 + 1/d2 + 1/d3)
            = .55 z1 + .27 z2 + .18 z3  for distances 1 2 3

        q may be one point, or a batch of points.
        eps: approximate nearest, dist <= (1 + eps) * true nearest
        p: use 1 / distance**p
        weights: optional multipliers for 1 / distance**p, of the same shape as q
        stat: accumulate wsum, wn for average weights

    How many nearest neighbors should one take ?
    a) start with 8 11 14 .. 28 in 2d 3d 4d .. 10d; see Wendel's formula
    b) make 3 runs with nnear= e.g. 6 8 10, and look at the results --
        |interpol 6 - interpol 8| etc., or |f - interpol*| if you have f(q).
        I find that runtimes don't increase much at all with nnear -- ymmv.

    p=1, p=2 ?
        p=2 weights nearer points more, farther points less.
        In 2d, the circles around query points have areas ~ distance**2,
        so p=2 is inverse-area weighting. For example,
            (z1/area1 + z2/area2 + z3/area3)
            / (1/area1 + 1/area2 + 1/area3)
            = .74 z1 + .18 z2 + .08 z3  for distances 1 2 3
        Similarly, in 3d, p=3 is inverse-volume weighting.

    Scaling:
        if different X coordinates measure different things, Euclidean distance
        can be way off.  For example, if X0 is in the range 0 to 1
        but X1 0 to 1000, the X1 distances will swamp X0;
        rescale the data, i.e. make X0.std() ~= X1.std() .

    A nice property of IDW is that it's scale-free around query points:
    if I have values z1 z2 z3 from 3 points at distances d1 d2 d3,
    the IDW average
        (z1/d1 + z2/d2 + z3/d3)
        / (1/d1 + 1/d2 + 1/d3)
    is the same for distances 1 2 3, or 10 20 30 -- only the ratios matter.
    In contrast, the commonly-used Gaussian kernel exp( - (distance/h)**2 )
    is exceedingly sensitive to distance and to h.

    """
    # anykernel( dj / av dj ) is also scale-free
    # error analysis, |f(x) - idw(x)| ? todo: regular grid, nnear ndim+1, 2*ndim

    def __init__( self, X, z, leafsize=10, stat=0 ):
        assert len(X) == len(z), "len(X) %d != len(z) %d" % (len(X), len(z))
        self.tree = KDTree( X, leafsize=leafsize )  # build the tree
        self.z = z
        self.stat = stat
        self.wn = 0
        self.wsum = None;

    def __call__( self, q, nnear=6, eps=0, p=1, weights=None ):
            # nnear nearest neighbours of each query point --
        q = np.asarray(q)
        qdim = q.ndim
        if qdim == 1:
            q = np.array([q])
        if self.wsum is None:
            self.wsum = np.zeros(nnear)

        self.distances, self.ix = self.tree.query( q, k=nnear, eps=eps )
        interpol = np.zeros( (len(self.distances),) + np.shape(self.z[0]) )
        jinterpol = 0
        for dist, ix in zip( self.distances, self.ix ):
            if nnear == 1:
                wz = self.z[ix]
            elif dist[0] < 1e-10:
                wz = self.z[ix[0]]
            else:  # weight z s by 1/dist --
                w = 1 / dist**p
                if weights is not None:
                    w *= weights[ix]  # >= 0
                w /= np.sum(w)
                wz = np.dot( w, self.z[ix] )
                if self.stat:
                    self.wn += 1
                    self.wsum += w
            interpol[jinterpol] = wz
            jinterpol += 1
        return interpol if qdim > 1  else interpol[0]