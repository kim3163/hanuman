#!/usr/bin/env python
#-*- coding:utf-8 -*-

import sys
import os
import signal
import fnmatch
import datetime as dt
import subprocess
import ConfigParser
import Mobigen.Common.Log as Log; Log.Init()
import paramiko

SHUTDOWN = False
def shutdown(sigNum, frame):
	global SHUTDOWN
	SHUTDOWN = True
	sys.stderr.write("Catch Signal :%s" % sigNum)
	sys.stderr.flush()

signal.signal(signal.SIGTERM, shutdown) # sigNum 15 : Terminate
signal.signal(signal.SIGINT, shutdown)  # sigNum  2 : Interrupt
signal.signal(signal.SIGHUP, shutdown)  # sigNum  1 : HangUp
signal.signal(signal.SIGPIPE, shutdown) # sigNum 13 : Broken Pipe


class ExceptionTracer(object):
	def __init__(self, cfg):
		self.cfg = cfg
		self.set_config()
		self.ex_dic = {}
		self.included_patterns = '*'
		self.excluded_patterns = ''
		now_time = dt.datetime.now()
		self.threshold_time = now_time - dt.timedelta(minutes=self.interval)
		self.time_list = []
		check_format = self.time_format[:self.time_format.find("%M") + 2]
		for x in range(self.interval):
			self.time_list.append((self.threshold_time + dt.timedelta(minutes=x)).strftime(check_format))

	def set_config(self):
		self.server = self.cfg.get('EXCEPTION_TRACER', 'SERVER_LIST')
		self.path = self.cfg.get('EXCEPTION_TRACER', 'PATH')
		#self.excluded_modules = self.cfg.get('EXCEPTION_TRACER', 'EXCLUDED_FILE')
		#self.excluded_modules = [x.strip() for x in self.excluded_modules.split(',')]
		self.included_patterns = self.cfg.get('EXCEPTION_TRACER', 'INCLUDED_PATTERN')
		if self.cfg.has_option('EXCEPTION_TRACER', 'EXCLUDED_PATTERN'):
			self.excluded_patterns = self.cfg.get('EXCEPTION_TRACER', 'EXCLUDED_PATTERN')

		self.time_format = self.cfg.get('EXCEPTION_TRACER', 'TIME_FORMAT')
		self.word 		= self.cfg.get('EXCEPTION_TRACER', 'WORD')
		self.USER 		= self.cfg.get('EXCEPTION_TRACER', 'USER')
		self.PASSWD  	= self.cfg.get('EXCEPTION_TRACER', 'PASSWD')
		self.SSH_PORT	= self.cfg.get('EXCEPTION_TRACER', 'SSH_PORT')

		# INTERVAL 은 최소단위가 1 시간
		self.interval = self.cfg.getint('EXCEPTION_TRACER', 'INTERVAL')

	def arrage_exception(self):
		cache = {}
		for x in self.ex_dic: # x : FileName
			cache[x] = {}
			for y in self.ex_dic[x]: # y : Exception Message
				lines = y.split('\n')
				key = ''
				for z in lines:
					if (", line" in z) and ("File " in z):
						key += z
				if key in cache[x].keys():
					cache[x][key].append(lines[0][:21])
				else:
					cache[x][key] = [lines[0][:21]]
		return cache

	def select_files(self):
		included = fnmatch.filter(os.listdir(self.path), '*')
		for x in self.included_patterns.split(','):
			included = fnmatch.filter(included, x.strip())

		excluded = []
		if self.excluded_patterns:
			for x in self.excluded_patterns.split(','):
				excluded = excluded + fnmatch.filter(os.listdir(self.path), x.strip())
			logs = list(set(included) - set(excluded))
		else:
			logs = included

		subjects= []

		for log in logs:
			log_path = os.path.join(self.path, log)
			file_mtime = dt.datetime.fromtimestamp(os.stat(log_path).st_mtime)
			if not os.path.isdir(log_path):
				if file_mtime > self.threshold_time:
					subjects.append(log)

		return subjects

	def SSHClinetConnection(self):
		client = self.client
		client.load_system_host_keys()
		client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
	try:
		client.connect(self.ip, username=self.uname, password=self.pw, port=self.port, timeout=10)
	except:
		client.connect(self.ip, username=self.uname, password=self.pw2, port=self.port, timeout=10)

	def select_files2(self):
		logs = []
		for (path, directory, files) in os.walk(self.path):
			included, excluded = [], []
			for x in self.included_patterns.split(','):
				included += fnmatch.filter(files, x.strip())
			for x in self.excluded_patterns.split(','):
				excluded += fnmatch.filter(files, x.strip())
			logs += map(lambda z : os.path.join(path, z), list(set(included) - set(excluded)))

		subjects = []
		for log_path in logs:
			file_mtime = dt.datetime.fromtimestamp(os.stat(log_path).st_mtime)
			if not os.path.isdir(log_path):
				if file_mtime > self.threshold_time:
					subjects.append(log_path)

		return subjects


	def run(self):
		__LOG__.Trace("----------[Collect]EXCEPTION LOG TRACING START----------")
		#paths = [os.path.join(self.path, x) for x in self.select_files()]
		paths = self.select_files2()
		__LOG__.Trace(self.path)
		status = 'OK'
		first_arg = "egrep '%s' %s" % ('|'.join(self.time_list), ' '.join(paths))
		second_arg = self.word
#		cmd = "%s | grep '%s'" % (first_arg, second_arg)
		cmd = "cat %s | grep '%s' | grep '%s'" % (self.path, second_arg, '\|'.join(self.time_list))
		cmd = cmd.replace('[', '\[', 2000)
		cmd = cmd.replace(']', '\]', 2000)

		__LOG__.Trace(cmd)

		p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		__LOG__.Trace(p)
		result_str = p.stdout.readlines()

		p = subprocess.Popen("cat /proc/sys/kernel/hostname",
							shell=True,stdout=subprocess.PIPE,
							stderr=subprocess.STDOUT)
		hostname = p.stdout.readline().strip()

		result = {}
		result[self.server] = {'STATUS' : status}
		result[self.server]['HOSTNAME'] = {"VALUE": [hostname]}
		result[self.server]['EXCEPTION'] = result_str

		__LOG__.Trace(result)
		
		return result

	def run_deprecated(self):
		__LOG__.Trace("----------[Collect]EXCEPTION LOG TRACING START----------")
		paths = self.select_files()
		status = 'OK'
		for x in paths:
			file_path = os.path.join(self.path, x)
			self.ex_dic[x] = []
			ex_count = 0
			line = True
			with open(file_path, 'r') as f:
				while line:
					message = ''
					line = f.readline()
					try:
						logged_time = dt.datetime.strptime(line[:21], '[%Y/%m/%d %H:%M:%S]')
					except:
						logged_time = dt.datetime.strptime('20001231235959', '%Y%m%d%H%M%S')
					
					if logged_time >= self.threshold_time:
						if '!!! Exception !!!' in line:
							message += line
							while (not ('Error:' in line) or ('error:' in line)) and line:
								line = f.readline()
								message += line
							self.ex_dic[x].append(message)

			if not self.ex_dic[x]:
				del self.ex_dic[x]

		arranged = self.arrage_exception()

		p = subprocess.Popen("cat /proc/sys/kernel/hostname", 
							shell=True,stdout=subprocess.PIPE, 
							stderr=subprocess.STDOUT)
		hostname = p.stdout.readline().strip()

		result = {}
		result[self.server] = {}
		result[self.server] = {'STATUS' : status}
		result[self.server]['HOSTNAME'] = {"VALUE": [hostname]}
		result[self.server]['EXCEPTION'] = arranged

		__LOG__.Trace(result)

		return result

def main():
	cfg = ConfigParser.ConfigParser()
	cfg.read(sys.argv[1])
	result = ExceptionTracer(cfg).run()
	for x in result['211.237.65.141']['EXCEPTION']:
		print x
	print result


if __name__ == '__main__':
	main()

