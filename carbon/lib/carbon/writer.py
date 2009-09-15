"""Copyright 2009 Chris Davis

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""


import os
import time
from os.path import exists, dirname
from time import sleep
from threading import Thread
from twisted.internet import reactor
from twisted.internet.task import LoopingCall
import whisper
from carbon.cache import MetricCache
from carbon.storage import getFilesystemPath, loadStorageSchemas
from carbon.conf import settings
from carbon.instrumentation import increment, append
from carbon import log


def optimalWriteOrder():
  "Generates metrics with the most cached values first and applies a soft rate limit on new metrics"
  metrics = [ (metric, len(datapoints)) for metric,datapoints in MetricCache.items() ]
  metrics.sort(key=lambda item: item[1], reverse=True) # by queue size, descending

  cacheSize = len(metrics)
  newCount = 0
  newLimit = int( cacheSize * settings.METRIC_CREATION_RATE )

  if newLimit < 10: # ensure we always do at least some creates
    newLimit = 10

  for metric, queueSize in metrics:
    dbFilePath = getFilesystemPath(metric)
    dbFileExists = exists(dbFilePath)

    if not dbFileExists:
      newCount += 1
      if newCount >= newLimit:
        continue

    try: # metrics can momentarily disappear from the MetricCache due to the implementation of MetricCache.store()
      datapoints = MetricCache.pop(metric)
    except KeyError:
      log.writer("MetricCache contention, skipping %s update for now" % metric)
      continue # we simply move on to the next metric when this race condition occurs

    yield (metric, datapoints, dbFilePath, dbFileExists)


def writeCachedDataPoints():
  "Write datapoints until the MetricCache is completely empty"
  while MetricCache:
    for (metric, datapoints, dbFilePath, dbFileExists) in optimalWriteOrder():

      if not dbFileExists:
        for schema in schemas:
          if schema.matches(metric):
            log.writer('new metric %s matched schema %s' % (metric, schema.name))
            archiveConfig = [archive.getTuple() for archive in schema.archives]
            break

        dbDir = dirname(dbFilePath)
        os.system("mkdir -p '%s'" % dbDir)

        log.writer("creating new database file %s" % dbFilePath)
        whisper.create(dbFilePath, archiveConfig)
        increment('creates')

      pointCount = len(datapoints)
      log.writer("writing %d datapoints for %s" % (pointCount, metric))

      try:
        t1 = time.time()
        whisper.update_many(dbFilePath, datapoints)
        updateTime = time.time() - t1
      except:
        log.err()
        increment('errors')
      else:
        increment('committedPoints', pointCount)
        append('updateTimes', updateTime)


def writeForever():
  while reactor.running:
    try:
      writeCachedDataPoints()
    except:
      log.err()

    sleep(1) # The writer thread only sleeps when the cache is empty or an error occurs


def reloadStorageSchemas():
  global schemas
  try:
    schemas = loadStorageSchemas()
  except:
    log.writer("Failed to reload storage schemas")
    log.err()


schemaReloadTask = LoopingCall(reloadStorageSchemas)


def startWriter():
  schemaReloadTask.start(60)
  reactor.callInThread(writeForever)
