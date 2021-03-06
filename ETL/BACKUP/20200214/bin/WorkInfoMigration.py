#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys

reload(sys)
sys.setdefaultencoding('UTF-8')

import signal
import time
from datetime import datetime
from datetime import timedelta
import ConfigParser
import glob
import json
import uuid
import shutil
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from apscheduler.schedulers.blocking import BlockingScheduler

import SftpClient as SFTPClient
import Mobigen.Common.Log as Log; Log.Init()
import subprocess
import dateutil.parser as dparser

workInfoCollector = None

def handler(signum, frame):
	__LOG__.Trace('signal : process shutdown')
	try :
		if workInfoCollector :
			workInfoCollector.shutdown()
	except : 
		__LOG__.Exception()

# SIGTERM
signal.signal(signal.SIGTERM, handler)
# SIGINT
signal.signal(signal.SIGINT, handler)
# SIGHUP
signal.signal(signal.SIGHUP, handler)
# SIGPIPE
signal.signal(signal.SIGPIPE, handler)


REQUEST_FAIL_CNT   = 0

class WorkInfoMigration :
	def __init__(self, cfg) :
		self.cfg 				= cfg
		self.WORKINFO_REPO 		= {}

		self._initConfig()

	def _initConfig(self) :
		self.systemName 		= self.cfg.get('MODULE_CONF', 'TACS_SYSTEM_NAME')
		self.workInfoBaseDir    = self.cfg.get('MODULE_CONF', 'TACS_WORKINFO_RAW')

		self.auditLogTempDir	= self.cfg.get('MODULE_CONF', 'TACS_AUDITLOG_TEMP')
		self.auditLogBaseDir	= self.cfg.get('MODULE_CONF', 'TACS_AUDITLOG_PATH')
		self.receivedWorkCode	= self.cfg.get('MODULE_CONF', 'RECEIVED_WORK_CODE')

		self.tangoWmWorkInfoUrl = self.cfg.get('MODULE_CONF', 'TANGO_WM_WORKINFO_URL')
		self.tangoWmEqpInfoUrl	= self.cfg.get('MODULE_CONF', 'TANGO_WM_EQPINFO_URL')
		self.xAuthToken			= self.cfg.get('MODULE_CONF', 'TANGO_WM_X_AUTH_TOKEN')

		self.host               = self.cfg.get('MODULE_CONF', 'TANGO_WM_SFTP_HOST')
		self.port               = int(self.cfg.get('MODULE_CONF', 'TANGO_WM_SFTP_PORT'))
		self.user               = self.cfg.get('MODULE_CONF', 'TANGO_WM_SFTP_USER')
		self.passwd             = self.cfg.get('MODULE_CONF', 'TANGO_WM_SFTP_PASSWD')

		self.scheduleInterval   = self.cfg.get('MODULE_CONF', 'SCHEDULE_INTERVAL_MIN')

		self.stdoutSleepTime	= int(self.cfg.get('MODULE_CONF', 'STDOUT_SLEEP_TIME'))
		self.migrationPath		= self.cfg.get('MODULE_CONF', 'COLLECT_MIGRATION_PATH')

		self.headers = {'x-auth-token' : self.xAuthToken, 'Content-Type' : 'application/json; charset=utf-8'}
		self.migration			= False

		self.errFilePath		= self.cfg.get('MODULE_CONF', 'ERROR_FILE_PATH')
		self.searchStartDate	= None
		self.searchEndDate		= None
		self.migrationProcFlag 	= False

	def _stdout(self, msg) :
		sys.stdout.write('stdout' + msg + '\n')
		sys.stdout.flush()
		__LOG__.Trace('stdout: %s' % msg)

	def _executeScheduler(self) :
		try :
			__LOG__.Trace('scheduler process start')
			fileNameList	= self._observFileNameList()

			# request workInfo
			workIdList 		= self._lookupWorkInfo()

			# request eqpInfo by workId
			self._lookupEqpInfo(workIdList)
		except :
			__LOG__.Exception()

	def _observFileNameList(self) :
		fileNameList = list()
		self._mkdirs(self.errFilePath)
		fileNameList = os.path.listdir(self.errFilePath)
		
		for oneFileName in fileNameList :
			try :
				dparser.parse(oneFileName)
			except ValueError as valErr :
				del fileNameList(oneFileName)

	def _lookupWorkInfo(self, fromDate = None, toDate = None, migration = False) :
		self.searchStartDate = fromDate
		self.searchEndDate	= toDate

		if not migration :
			searchEndDateObj  	= datetime.now()
			#searchStartDateObj  = datetime(searchEndDateObj.year, searchEndDateObj.month, searchEndDateObj.day, searchEndDateObj.hour, (searchEndDateObj.minute - int(self.scheduleInterval)))
			searchStartDateObj	= searchEndDateObj - timedelta(minutes=1)

 			self.searchStartDate  	= searchStartDateObj.strftime('%Y%m%d%H%M')
 			self.searchEndDate 		= searchEndDateObj.strftime('%Y%m%d%H%M')
	
		__LOG__.Trace('lookup workInfo from({}) ~ to({})'.format(self.searchStartDate, self.searchEndDate))

 		url = self.tangoWmWorkInfoUrl.format(self.systemName, self.searchStartDate, self.searchEndDate)
 		__LOG__.Trace('request workInfo url: {}'.format(url))

		rawDict = self._requestGet(url)
		return self._loadWorkInfo(rawDict)

	def _lookupEqpInfo(self, workIdList) :
		if not workIdList :
			__LOG__.Trace('workIdList is empty')
		else :
			logDictList = list()
			yyyyMMdd	= None
			eventDate	= None

			for oneWorkId in workIdList :
				url = self.tangoWmEqpInfoUrl.format(self.systemName, oneWorkId)
				__LOG__.Trace('request eqpInfo url: {}'.format(url))

				rawDict = self._requestGet(url)
				logDict, yyyyMMdd, eventDate = self._loadEqpInfo(oneWorkId, rawDict, logDictList)
				logDictList.append(logDict)
			if rawDict :
				self._writeTacsHistoryFile(yyyyMMdd, eventDate, logDictList)
			else :
				__LOG__.Trace('eqpInfo Dict is None {}'.format(rawDict))

	def _requestGet(self, url, verify = False) :
		rawDict 	= None
		response	= None

		try :
			response 	= requests.get(url = url, headers = self.headers, verify = verify)
			
			if response != None and response.status_code == 200 :
				rawDict = response.json()
			else :
				__LOG__.Trace('!!! Exception !!! requestGet failed. statusCode: {}'.format(response.status_code))
				self.createDateFile('{}_{}'.format(self.searchStartDate, self.searchEndDate))
				pass

		except :
			__LOG__.Exception()
			self.createDateFile('{}_{}'.format(self.searchStartDate, self.searchEndDate))
			pass

		return rawDict

	def _loadWorkInfo(self, rawDict) :
		if rawDict :
			__LOG__.Trace('workInfo rawData: {}'.format(rawDict))
			workIdList = []

			if type(rawDict['workInfo']) is list :
				for oneWorkInfo in rawDict['workInfo'] :
					workId = oneWorkInfo['workId']
					__LOG__.Trace('workId: {}'.format(workId))
					if workId is None or not workId :
						__LOG__.Trace('invalid workId({})'.format(workId))
						continue

					workIdList.append(workId)
															
					wrapper 			= {}
					wrapper['workInfo'] = oneWorkInfo
					
					workEvntDate = datetime.now().strftime('%Y%m%d%H%M%S')
					wrapper['workInfo']['workEvntDate'] = workEvntDate					

					self.WORKINFO_REPO[workId] = wrapper
				__LOG__.Trace('WORKINFO_REPO: {}'.format(self.WORKINFO_REPO))
			else :
				__LOG__.Trace('Unsupported type: {}'.format(type(rawDict['workInfo'])))
				pass

			return workIdList
		else :
			__LOG__.Trace('workInfo rawData is None')
			return None

	def _loadEqpInfo(self, oneWorkId, rawDict, logDictList) :
		logDict 	= dict()
		yyyyMMdd	= None
		eventDate	= None

		if rawDict :
			__LOG__.Trace('eqpInfo rawData: {}'.format(rawDict))
			if 'eqpInfo' in rawDict and type(rawDict['eqpInfo']) is list :
				scriptFileList = []
				wrapper = self.WORKINFO_REPO[oneWorkId]
				if wrapper : 
					wrapper['eqpInfo'] = rawDict['eqpInfo']
					for oneEqpInfoDict in rawDict['eqpInfo'] :
						if 'scriptInfo' in oneEqpInfoDict :
							scriptInfoList = oneEqpInfoDict['scriptInfo']
								
							if scriptInfoList :
								for oneScriptInfoDict in scriptInfoList :
									filePathname = oneScriptInfoDict['atchdPathFileNm']
									if filePathname :
										remoteFilepath, remoteFilename = os.path.split(filePathname)
										__LOG__.Trace('remoteFilepath({}), remoteFilename({})'.format(remoteFilepath, remoteFilename))
										scriptFileDict = {}
										scriptFileDict['remoteFilepath'] = remoteFilepath
										scriptFileDict['remoteFilename'] = remoteFilename

										scriptFileList.append(scriptFileDict)
									else :
										__LOG__.Trace('workId({})/eqpNm({}) atchdPathFileNm({}) is invalid'.format(oneWorkId, oneEqpInfoDict['eqpNm'], filePathname))
										pass
							else :
								__LOG__.Trace('workId({})/eqpNm({}) scriptInfoList({}) is invalid'.format(oneWorkId, oneEqpInfoDict['eqpNm'], scriptInfoList))
						else :
							__LOG__.Trace('workId({})/eqpNm({}) scriptInfo does not exist in eqpInfo'.format(oneWorkId, oneEqpInfoDict['eqpNm']))
							pass
				else :
					__LOG__.Trace('no registered workId({}) in WORKINFO_REPO'.format(oneWorkId))
					return
	
				__LOG__.Trace('scriptFileList: {}'.format(scriptFileList))
				eventDate 	= wrapper['workInfo']['workEvntDate']
				yyyyMMdd 	= datetime.strptime(eventDate, '%Y%m%d%H%M%S').strftime('%Y%m%d')
				__LOG__.Trace('eventDate({}), yyyyMMdd({})'.format(eventDate, yyyyMMdd))
				self._getScriptFiles(yyyyMMdd, oneWorkId, scriptFileList)

				logDict = self._writeTangoWorkFile(yyyyMMdd, eventDate, oneWorkId, wrapper)

				self._removeCompleteWorkInfo(oneWorkId)
			else :
				__LOG__.Trace('Unsupported type: {}'.format('eqpInfo' in rawDict if type(rawDict['eqpInfo']) else None ))
				pass
		else :
			__LOG__.Trace('workId({}), eqpInfo rawData is None'.format(oneWorkId))
			pass

		return logDict, yyyyMMdd, eventDate

	def _getScriptFiles(self, yyyyMMdd, workId, scriptFileList) :
		if not scriptFileList :
			__LOG__.Trace('scriptFileList({}) is empty'.format(scriptFileList))
			return

		try :
			tacsWorkInfoPath = os.path.join(self.workInfoBaseDir, yyyyMMdd, workId)
			self._mkdirs(tacsWorkInfoPath)

			sftpClient = SFTPClient.SftpClient(self.host, self.port, self.user, self.passwd)
			for oneScriptFileDict in scriptFileList :
				remoteFilepath 	= oneScriptFileDict['remoteFilepath']
				remoteFilename 	= oneScriptFileDict['remoteFilename'] 

				sftpClient.download(remoteFilepath, remoteFilename, tacsWorkInfoPath)
				__LOG__.Trace('scriptFile from({}) -> to({}) download succeed'.format(os.path.join(remoteFilepath, remoteFilename), os.path.join(tacsWorkInfoPath, remoteFilename)))

			sftpClient.close()
		except Exception as ex :
			__LOG__.Trace('scriptFile download proccess failed {}'.format(ex))
			self._removeCompleteWorkInfo(workId)
			raise ex
	
	def _writeTangoWorkFile(self, yyyyMMdd, eventDate, workId, wrapper) :
		logDict = {}
		try :
			tacsWorkInfoPath = os.path.join(self.workInfoBaseDir, yyyyMMdd, workId)
			self._mkdirs(tacsWorkInfoPath)

			contents = json.dumps(wrapper, ensure_ascii=False)
			__LOG__.Trace('contents: {}'.format(contents))
			createFilePath = os.path.join(tacsWorkInfoPath, '{}_{}_META.json'.format(eventDate, workId))
			self._createFile(createFilePath, contents)
			logDict['tacsLnkgRst'] = 'OK'

			if self.migration :
				__LOG__.Trace( ['mf','30000', 'put', 'dbl', 'stdoutfile://{}'.format(createFilePath)] )	
				subprocess.call(['mf', '30000', 'put,dbl,stdoutfile://{}'.format(createFilePath)])
			else :
				time.sleep(self.stdoutSleepTime)
				self._stdout('file://{}'.format(createFilePath))
		except Exception as ex :
			__LOG__.Trace('workFile write process failed {}'.format(ex))
			logDict['tacsLnkgRst'] = 'FAIL'
			logDict['tacsLnkgRsn'] = ex.args
			self._removeCompleteWorkInfo(workId)
			raise ex
		finally :
			logDict['evntTypCd'] 	= self.receivedWorkCode
			logDict['evntDate'] 	= eventDate
			logDict['workId']		= workId
			logDict['lnkgEqpIp']	= ''

		return logDict

#			self._writeTacsHistoryFile(yyyyMMdd, eventDate, logDict)

	def _writeTacsHistoryFile(self, yyyyMMdd, eventDate, logDictList) :
		if logDictList :
			__LOG__.Trace('received workInfo history: {}'.format(logDictList))
			try :
				tacsHistoryTempPath = os.path.join(self.auditLogTempDir, 'AUDIT_{}'.format(self.receivedWorkCode))
				self._mkdirs(tacsHistoryTempPath)
				contentList	= list()

				for oneLogDict in logDictList :
					content  = json.dumps(oneLogDict, ensure_ascii=False)
					contentList.append(content)		

				contents = '\n'.join(contentList)
			
				__LOG__.Trace('contents: {}'.format(contents))

				tacsHistoryFilename = self._getTacsHistoryFilename(yyyyMMdd, eventDate)
				__LOG__.Trace('tacsHistoryFilename: {}'.format(tacsHistoryFilename))
				self._createFile(os.path.join(tacsHistoryTempPath, tacsHistoryFilename), contents) 
				
				tacsHistoryPath = os.path.join(self.auditLogBaseDir, 'AUDIT_{}'.format(self.receivedWorkCode))
				self._mkdirs(tacsHistoryPath)

				shutil.move(os.path.join(tacsHistoryTempPath, tacsHistoryFilename), os.path.join(tacsHistoryPath, tacsHistoryFilename))
				__LOG__.Trace('tacsHistory file move from {} -> to {} succeed'.format(os.path.join(tacsHistoryTempPath, tacsHistoryFilename), os.path.join(tacsHistoryPath, tacsHistoryFilename)))
			except Exception as ex :
				__LOG__.Trace('tacsHistory {} load process failed {}'.format(logDict, ex))
		else :
			__LOG__.Trace('received workInfo history({}) is invalid'.format(logDict))

	def _mkdirs(self, directory) :
		__LOG__.Trace('{} isExists: {}'.format(directory, os.path.exists(directory)))
		if not os.path.exists(directory) :
			__LOG__.Trace('create directories {}'.format(directory))
			os.makedirs(directory)

	def _createFile(self, filePath, contents) :
		f = None
		try :
			f = open(filePath, 'w')
			f.write(contents)
			__LOG__.Trace('{} file is created'.format(filePath))
		except Exception as ex :
			__LOG__.Trace('{} to file process failed {}'.format(contents, ex))
			raise ex
		finally :
			if f :
				f.close()

	def _getTacsHistoryFilename(self, yyyyMMdd, eventDate) :
		HHmm 				= datetime.strptime(eventDate, '%Y%m%d%H%M%S').strftime('%H%M')
		tacsHistoryFilename = '{}_{}_{}.audit'.format(yyyyMMdd, HHmm, uuid.uuid4())
		return tacsHistoryFilename

	def _removeCompleteWorkInfo(self, workId) :
		if workId in self.WORKINFO_REPO :
			del self.WORKINFO_REPO[workId]
			__LOG__.Trace('workId({}), WORKINFO_REPO: {}'.format(workId, self.WORKINFO_REPO))

	def shutdown(self) :
		try :
			if self.scheduler :
				#self.scheduler.remove_job('workInfo_scheduler')
				self.scheduler.shutdown()
				__LOG__.Trace('schduler is terminated')
			else :
				_LOG__.Trace('scheduler is None')
		except Exception as ex :
			__LOG__.Trace('shutdown failed {}'.format(ex))

	def run(self) :

		self.scheduler = BlockingScheduler()
		self.scheduler.add_job(self._executeScheduler, 'cron', minute='*/{}'.format(self.scheduleInterval), second='0', id='workInfo_scheduler', max_instances=2)
		self.scheduler.start()

def main() :
	argvLength = len(sys.argv)
	if argvLength < 3 :
		print '''
[ERROR] WorkInfoMigration argv required at least 3
++ Usage 
++++ scheduler : module section cfgfile
'''
		return

	module 	= os.path.basename(sys.argv[0])
	section = sys.argv[1]
	cfgfile = sys.argv[2]
	
	cfg = ConfigParser.ConfigParser()
	cfg.read(cfgfile)

	logPath = cfg.get("GENERAL", "LOG_PATH")
	logFile = os.path.join(logPath, "%s_%s.log" % (module, section))

	logCfgPath = cfg.get("GENERAL", "LOG_CONF")

	logCfg = ConfigParser.ConfigParser()
	logCfg.read(logCfgPath)

	Log.Init(Log.CRotatingLog(logFile, logCfg.get("LOG", "MAX_SIZE"), logCfg.get("LOG", "MAX_CNT") ))

	global workInfoMigration

	workInfoMigration = WorkInfoMigration(cfg)
	workInfoMigration.run()

	__LOG__.Trace('main is terminated')


if __name__ == '__main__' :
	try :
		main()
	except :
		__LOG__.Exception()
