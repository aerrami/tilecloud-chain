# -*- coding: utf-8 -*-

import logging
import yaml
from itertools import imap, ifilter
from hashlib import sha1

import psycopg2
from shapely.wkb import loads as loads_wkb
from shapely.geometry import Polygon
import boto.sqs
from boto.sqs.jsonmessage import JSONMessage

from tilecloud import Tile
from tilecloud.grid.free import FreeTileGrid
from tilecloud.filter.error import LogErrors, MaximumConsecutiveErrors, DropErrors


class TileGeneration:
    geom = None

    def __init__(self, config_file, layer_name=None):
        self.config = yaml.load(file(config_file))

        self.grids = self.config['grids']
        for gname, grid in self.config['grids'].items():
            grid['obj'] = FreeTileGrid(
                resolutions=grid['resolutions'],
                max_extent=grid['bbox'],
                tile_size=grid['tile_size'])

        default = self.config['layer_default']
        self.layers = {}
        for lname, layer in self.config['layers'].items():
            layer_object = {}
            for k, v in default.items():
                layer_object[k] = v

            for k, v in layer.items():
                layer_object[k] = v

            layer_object['grid_ref'] = self.grids[layer_object['grid']]

            self.layers[lname] = layer_object

        self.layer = None
        if layer_name:
            self.layer = self.layers[layer_name]

        self.caches = self.config['caches']

    def get_grid(self, name=None):
        if not name:
            name = self.layer['grid']

        return self.grids[name]

    def get_sqs_queue(self):
        config_sqs = self.config['forge']['sqs']
        connection = boto.sqs.connect_to_region(config_sqs['region_name'])
        queue = connection.create_queue(config_sqs['queue_name'])
        queue.set_message_class(JSONMessage)
        return queue

    def get_geom(self, bbox=None):
        if not self.geom:
            geom = None
            if 'connection' in self.layer and 'sql' in self.layer:
                conn = psycopg2.connect(self.layer['connection'])
                cursor = conn.cursor()
                cursor.execute("SELECT ST_AsBinary(ST_Collect((SELECT " +
                        self.layer['sql'].strip('" ') + "))")
                geom = loads_wkb(cursor.fetchone()[0])
            elif bbox:
                extent = (float(i) for i in bbox.split(','))
                geom = Polygon((
                        (extent[0], extent[1]),
                        (extent[0], extent[3]),
                        (extent[2], extent[3]),
                        (extent[2], extent[1]),
                    ))
            self.geom = geom
        return self.geom

    def add_geom_filter(self):
        # gets the geom on with one we should generate tiles
        geom = self.get_geom()

        if geom:
            self.ifilter(IntersectGeometryFilter(
                    grid=self.get_grid(),
                    geom=geom))

    def add_error_filters(self, logger):
        self.imap(LogErrors(logger, logging.ERROR,
                "Error in tile: %(tilecoord)s, %(error)r"))
        if 'maxconsecutive_errors' in self.config['generation']:
            self.imap(MaximumConsecutiveErrors(
                    self.config['generation']['maxconsecutive_errors']))
        self.ifilter(DropErrors())

    def set_metatilecoords(self, metatilecoords):
        self.tilestream = (
            Tile(metatilecoord) for metatilecoord in metatilecoords)

    def set_store(self, store):
        self.tilestream = store.list()

    def get(self, store):
        self.tilestream = store.get(self.tilestream)

    def put(self, store):
        self.tilestream = store.put(self.tilestream)

    def delete(self, store):
        self.tilestream = store.delete(self.tilestream)

    def imap(self, filter):
        self.tilestream = imap(filter, self.tilestream)

    def ifilter(self, filter):
        self.tilestream = ifilter(filter, self.tilestream)


class HashDropper(object):
    """
    Create a filter to remove the tiles data where they have
    the specified size and hash.

    Used to drop the empty tiles.

    The ``store`` is used to delete the empty tiles.
    """

    def __init__(self, size, sha1code, store=None):
        self.size = size
        self.sha1code = sha1code
        self.store = store

    def __call__(self, tile):
        if len(tile.data) != self.size or \
                sha1(tile.data).hexdigest() != self.sha1code:
            return tile
        else:
            if self.store:
                self.store.delete_one(tile)
            return None


class HashLogger(object):  # pragma: no cover
    """
    Log the tile size and hash.
    """

    def __init__(self, logger):
        self.logger = logger

    def __call__(self, tile):
        self.logger.info("size: %i, hash: %i" % (len(tile.data),
                 sha1(tile.data).hexdigest()))
        return tile


class IntersectGeometryFilter(object):

    def __init__(self, grid, geom=None):
        self.grid = grid
        self.geom = geom or self.bbox_polygon(self.grid.max_extent)

    def __call__(self, tile):
        intersects = self.bounds_polygon(
                self.grid.bounds(tile.tilecoord)). \
                intersects(self.geom)
        return tile if intersects else None

    def bbox_polygon(self, bbox):
        return Polygon((
                (bbox[0], bbox[1]),
                (bbox[0], bbox[3]),
                (bbox[2], bbox[3]),
                (bbox[2], bbox[1])))

    def bounds_polygon(self, bounds):
        return self.bounds_polygon((
                bounds[0].start, bounds[1].start,
                bounds[0].stop, bounds[1].stop))