﻿"""
ioHub
.. file: ioHub/client.py

Copyright (C) 2012 Sol Simpson
Distributed under the terms of the GNU General Public License (GPL version 3 or any later version).

.. moduleauthor:: Sol Simpson <sol@isolver-software.com> + contributors, please see credits section of documentation.
.. fileauthor:: Sol Simpson <sol@isolver-software.com>
"""

import os,sys
import time
import subprocess
from collections import deque
import struct

from gevent import socket
import numpy as N
import psutil

try:
    from yaml import load
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    print "*** Using Python based YAML Parsing"
    from yaml import Loader, Dumper

import ioHub
from ioHub.devices import Computer, EventConstants, DeviceEvent
from ioHub.devices.experiment import MessageEvent
from experiment import pumpLocalMessageQueue

MAX_PACKET_SIZE=64*1024

currentSec= Computer.currentSec

class ioHubConnectionException(Exception):
    pass

class SocketConnection(object):
    def __init__(self,local_host=None,local_port=None,remote_host=None,remote_port=None,rcvBufferLength=1492, broadcast=False, blocking=0, timeout=0,coder=None):
        self._local_port= local_port
        self._local_host = local_host
        self._remote_host= remote_host
        self._remote_port = remote_port
        self._rcvBufferLength=rcvBufferLength
        self.lastAddress=None
        self.sock=None
        self.initSocket()
        self.coder=None
        self.feed=None
        self.configCoder(coder)

    def initSocket(self,broadcast=False,blocking=0, timeout=0):
        #print 'Default SocketConnection being used'
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if broadcast is True:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack('@i', 1))

        if blocking is not 0:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.sock.settimeout(timeout)
        self.sock.setblocking(blocking)

    #noinspection PyArgumentList
    def configCoder(self,coder):
        #print "** In configCoder...", coder
        if coder:
            if coder == 'ujson':
                import ujson
                self.coder=ujson
                self.pack=ujson.encode
                self.unpack=ujson.decode
                #print 'ujson:', self.pack
            elif coder =='msgpack':
                import msgpack
                self.coder=msgpack
                self.packer=msgpack.Packer()
                self.unpacker=msgpack.Unpacker(use_list=True)
                self.pack=self.packer.pack
                self.feed=self.unpacker.feed
                self.unpack=self.unpacker.unpack
                #print 'msgpack:', self.pack
            else:
                raise Exception ("Unknown coder type: %s. Must be either 'ujson' or 'msgpack'"%(str(coder),))

    def sendTo(self,data,address=None):
        #print 'DATA [%s] %s %s'%(data,self._remote_host, str(self._remote_port))
        if address is None:
            address=self._remote_host, self._remote_port
        d=self.pack(data)
        byte_count=len(d)+2
        #print 'Sending byte count of ',len(d)+2,type(d)
        self.sock.sendto(d+'\r\n',address)
        return byte_count

    #noinspection PyArgumentList
    def receive(self):
        try:
            data, address = self.sock.recvfrom(self._rcvBufferLength)
            self.lastAddress=address
            if self.feed: # using msgpack
                self.feed(data[:-2])
                result=self.unpack()
                if result[0] == 'IOHUB_MULTIPACKET_RESPONSE':
                    num_packets=result[1]

                    for p in xrange(num_packets-1):
                        data, address = self.sock.recvfrom(self._rcvBufferLength)
                        self.feed(data)

                    data, address = self.sock.recvfrom(self._rcvBufferLength)
                    self.feed(data[:-2])
                    result=self.unpack()
                return result,address
            else:   # using ujson
                return self.unpack(data[:-2]),address
        except Exception as e:
            ioHub.printExceptionDetailsToStdErr()
            raise ioHubConnectionException(" IO_HUB_ERROR ", " ioHubConnection socket.receive", e)


    def close(self):
        self.sock.close()


class UDPClientConnection(SocketConnection):
    def __init__(self,remote_host='127.0.0.1',remote_port=9000,rcvBufferLength = MAX_PACKET_SIZE,broadcast=False,blocking=1, timeout=1, coder=None):
        SocketConnection.__init__(self,remote_host=remote_host,remote_port=remote_port,rcvBufferLength=rcvBufferLength,broadcast=broadcast,blocking=blocking, timeout=timeout,coder=coder)
    def initSocket(self,**kwargs):
        #print 'UDPClientConnection being used'
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, MAX_PACKET_SIZE)

#
# The ioHubDeviceView is the ioHub client side representation of an ioHub device.
# It has a dynamically created list of methods that can be called
# (matching the list of methods for the device in the ioHub.devices module),
# however here, each results in an RPC call to the ioHub for the device, which returns
# the result.
#
class DeviceRPC(object):
    def __init__(self,sendToHub,device_class,method_name):
        self.device_class=device_class
        self.method_name=method_name
        self.sendToHub=sendToHub

    def __call__(self, *args,**kwargs):
        r = self.sendToHub(('EXP_DEVICE','DEV_RPC',self.device_class,self.method_name,args,kwargs))
        r=r[1:]
        if len(r)==1:
            r=r[0]

        if r and self.method_name == 'getEvents':
            asType='dict'
            if 'asType' in kwargs:
                asType=kwargs['asType']

            if asType == 'list':
                return r
            else:
                conversionMethod=None
                if asType == 'dict':
                    conversionMethod=ioHubConnection._eventListToDict
                elif asType == 'object':
                    conversionMethod=ioHubConnection._eventListToObject

                if conversionMethod:
                    events=[]
                    for el in r:
                        events.append(conversionMethod(el))
                    return events

        return r

class ioHubDeviceView(object):
    """
    ioHubDeviceView is used by the ioHubConnection class to create a PsychoPy Process side representation
    of each ioHub Server Process device that has been defined in the ioHub configuration file for the
    current experiment. You never create instances of this class directly; they are created for you by the
    ioHubConnection class when it connects to the ioHub Process.

    The ioHubDeviceView provides methods to access the device name, instanceCode, and , associated
    ioHub Device Class.

    When the ioHubConnection class instance is created for your experiment, each device defined in
    the ioHub configuration file has a ioHub.devices.Device object created on the ioHub Server process,
    as well as a ioHubDeviceView object created on the PsychoPy Process that can be accessed from your
    experiment script.

    The ioHubConnection.devices attribute provides access to each ioHubDeviceView object that has been created
    via an attribute that has the same name as the name provided for the device in the ioHub configuration file.

    For example, if the ioHub configuration file for an experiment contains a keyboard and mouse device like:

    # start .yaml file snippet

    - device:
        device_class: Keyboard
        name: kb
        saveEvents: True
        streamEvents: True
        event_buffer_length: 256
    - device:
        device_class: Mouse
        name: mouse
        saveEvents: True
        streamEvents: True
        event_buffer_length: 256

    # end .yaml file snippet

    then two ioHubDeviceView objects will be created, one called 'kb' for the Keyboard Device, and a second called
    'mouse' for the Mouse Device.

    These can be accessed via:

        experimentKeyboard = ioHubConnectionInstance.devices.kb
        experimentMouse = ioHubConnectionInstance.devices.mouse

    If your PsychoPy script is running in a class that extends ioHub.experiment.ioHubExperimentRuntime, the
    devices can be accessed via:

         experimentKeyboard = self.hub.devices.kb
         experimentMouse = self.hub.devices.mouse


    Each ioHubDeviceView that is created when the ioHubConnection has connected to the ioHub Process, queries
    the ioHub Process for the current list of public method names for the given device_class. This list of names,
    which can be accessed by calling,

        methodNameList=ioHubDeviceViewInstance.getDeviceInterface()

    is used to provide the PsychoPy Process interface to the given device type. This allows a PsychoPy / ioHub
    experiment to call device methods on the ioHub Process as if the device method calls were being made locally.

    For example, to access a list of new keyboard events from the keyboard device, assuming you are running within a
    class that extends ioHub.experiment.ioHubExperimentRuntime, you would use the following code:

        kb = self.hub.devices.kb
        kb_events=kb.getEvents()

        print 'Keyboard Events', kb_events

    To set the current position of the system mouse cursor to be screen center, you could write the following:

        mouse = self.hub.devices.mouse
        print 'Mouse Position Before Update:', mouse.getPosition()
        mouse.setPosition((0,0))
        print 'Mouse Position After Update to (0,0):', mouse.getPosition()
    """
    def __init__(self,hubClient,name,dclass):
        self.hubClient=hubClient
        self.name=name
        self.device_class=dclass
        self._preRemoteMethodCallFunctions=dict()
        self._postRemoteMethodCallFunctions=dict()

        r=self.hubClient.sendToHubServer(('EXP_DEVICE','GET_DEV_INTERFACE',dclass))
        self._methods=r[1]

    def __getattr__(self,name):
        if name in self._methods:
            if name in self._preRemoteMethodCallFunctions:
                f,ka=self._preRemoteMethodCallFunctions[name]
                f(ka)
            r = DeviceRPC(self.hubClient.sendToHubServer,self.device_class,name)
            if name in self._postRemoteMethodCallFunctions:
                f,ka=self._postRemoteMethodCallFunctions[name]
                f(ka)
            return r
        raise AttributeError(self,name)

    def setPreRemoteMethodCallFunction(self,methodName,functionCall,**kwargs):
        self._preRemoteMethodCallFunctions[methodName]=(functionCall,kwargs)

    def setPostRemoteMethodCallFunction(self,methodName,functionCall,**kwargs):
        self._postRemoteMethodCallFunctions[methodName]=(functionCall,kwargs)

    def getName(self):
        """
        Gets the name given to the device in the ioHub configuration file.
        ( the device: name: property )

        Args: None
        Return (str): the user defined label / name of the device
        """
        return self.name

    def getIOHubDeviceClass(self):
        """
        Gets the ioHub Server Device class given associated with the ioHubDeviceView.
        This is specified for a device in the ioHub configuration file.
        ( the device: device_class: property )

        Args: None
        Return (class): the ioHub Server Device class associated with this ioHubDeviceView
        """
        return self.device_class

    def getDeviceInterface(self):
        """
        getDeviceInterface returns a list containing the names of all methods that are callable
        for the ioHubDeviceView object. Only public methods are considered to be valid members of
        the devices interface. (so any method beginning with a '_' is not included.

        Args: None

        Return (tuple): the list of method names that make up the ioHubDeviceView interface.
        """
        return self._methods


class ioHubDevices(object):
    """
    ioHubDevices is a class that contains an attribute (dynamically created) for each device that is created in the ioHub.
    These devices are each of type ioHubDeviceView. The attribute name for the device is the user name given to
    the device by the user in the ioHub config file label: field, so it must be a valid python attribute name.

    Each ioHubDeviceView itself has a list of methods that can be called (matching the list of public methods for the
    device in the ioHub.devices module), however here, each results in an IPC call to the ioHub Server for the device,
    which returns the result to the experiment process.

    A user never uses this class directly, it is used internallby by the ioHubVlient class to dynamically build out
    the experiment process side representation of the ioHub Server device set.
    """
    def __init__(self,hubClient):
        self.hubClient=hubClient

class ioHubConnection(object):
    """
    ioHubConnection is the main experiment process side class that is responsible to communicating
    with the ioHub Process.

    ioHubConnection is responsible for for creating the ioHub Server Process, sending message requests to the
    ioHub Server, and reading the ioHub Server process reply. This class can also tell the ioHub server when to
    close down and disconnect. The ioHubConnection class also has an experiment side representation of the devices
    that have been registered with the ioHub Process for monitoring. These device objects can be accessed via the
    ioHubConnection's devices attribute. For example, to print the available methods for each device:

        for d in self.devices:
            print d, d.getDeviceInterface()
            print '--------------'

    If using the ioHubExperimentRuntime utility class to create your experiment, an instance of ioHubConnection
    is created for you automatically and is accessible via self.hub. So to perform the same task as above,
    but from within the ioHubExperimentRuntime.run() method:

        for d in self.hub.devices:
            print d, d.getDeviceInterface()
            print '--------------'

    """
    _replyDictionary=dict()
    def __init__(self,ioHubConfig={},ioHubConfigAbsPath=None):
        self._initial_clock_offset=ioHub.highPrecisionTimer()
        Computer.globalClock=ioHub.ioClock(self,self._initial_clock_offset,False)

        if ioHubConfigAbsPath is not None and (ioHubConfig is None or len(ioHubConfig) == 0):
            ioHubConfig=load(file(ioHubConfigAbsPath,u'r'), Loader=Loader)

            
        # udp port setup
        self.udp_client = UDPClientConnection(coder=ioHubConfig.get('ipcCoder','msgpack'))

        # the dynamically generated object that contains an attribute for
        # each device registed for monitoring with the ioHub server so
        # that devices can be accessed experiment process side by device name.
        self.devices=ioHubDevices(self)

        # a dictionary that holds the same devices represented in .devices, 
        # but stored in a dictionary using the device
        # name as the dictionary key
        self.deviceByLabel=dict()
        
        
        # A circular buffer used to hold events retrieved from self.getEvents() during 
        # self.delay() calls. self.getEvents() appends any events in the allEvents
        # buffer to the result of the hub.getEvents() call that is made.  
        self.allEvents=deque(maxlen=512)

        # attribute to hold the current experiment ID that has been 
        # created by the ioHub ioDataStore if saving data to the
        # ioHub hdf5 file type.
        self.experimentID=None

        # attribute to hold the current experiment session ID that has been
        # created by the ioHub ioDataStore if saving data to the
        # ioHub hdf5 file type.
        self.experimentSessionID=None
            
        self._startServer(ioHubConfig, ioHubConfigAbsPath)

    def _startServer(self,ioHubConfig=None, ioHubConfigAbsPath=None):
        """
        Starts the ioHub Server Process, storing it's process id, and creating the experiment side device representation
        for IPC access to public device methods.
        """
        
        if ioHubConfigAbsPath is None and (ioHubConfig is None or len(ioHubConfig) == 0):
            ioHubConfigAbsPath=os.path.join(ioHub.IO_HUB_DIRECTORY,'default_config.yaml')
        elif (ioHubConfig is not None and len(ioHubConfig)>0) and ioHubConfigAbsPath is None:
            ioHub.print2err("ERROR: ioHubConnection does not yet support configuring server via python dictionary. Only .yaml file configuration is supported at this time.")
            sys.exit(1)
        
        #print 'ioHubConfig:', ioHubConfig
        #print 'ioHubConfigAbsPath: ', ioHubConfigAbsPath
        
        run_script=os.path.join(ioHub.IO_HUB_DIRECTORY,'server.py')
        subprocessArgList=[sys.executable, run_script,"%.6f"%(self._initial_clock_offset,),ioHubConfigAbsPath]

        #print 'run_script:', run_script
        #print 'subprocessArgList: ', subprocessArgList

        # check for existing ioHub Process based on process if saved to file
        iopFileName=os.path.join(ioHub.IO_HUB_DIRECTORY,'.iohpid')
        if os.path.exists(iopFileName):
            try:
                iopFile= open(iopFileName,'r')
                line=iopFile.readline()
                iopFile.close()
                os.remove(iopFileName)
                other,iohub_pid=line.split(':')
                iohub_pid=int(iohub_pid.strip())
                old_iohub_process=psutil.Process(iohub_pid)
                if old_iohub_process.name == 'python.exe':
                    old_iohub_process.kill()
            except psutil.NoSuchProcess:
                pass
            except:
                ioHub.printExceptionDetailsToStdErr()

        #print 'STARTING IOSERVER.....'
        # start subprocess, get pid, and get psutil process object for affinity and process priority setting
        self._server_process = subprocess.Popen(subprocessArgList,stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        Computer.ioHubServerProcessID = self._server_process.pid
        Computer.ioHubServerProcess=psutil.Process(Computer.ioHubServerProcessID)

        # wait for server to send back 'IOHUB_READY' text over stdout, indicating it is running
        # and ready to receive network packets
        hubonline=False
        server_output='hi there'
        ctime = Computer.globalClock.getTime
        timeout_time=ctime()+10.0 # timeout if ioServer does not reply in 10 seconds
        while server_output and ctime()<timeout_time:
            isDataAvail=self._serverStdOutHasData()
            if isDataAvail is True:
                server_output=self._readServerStdOutLine().next()
                if server_output.rstrip() == 'IOHUB_READY':
                    hubonline=True
                    #print "Ending Serving connection attempt due to timeout...."
                    break
            else:
                time.sleep(0.0001)
                
        # If ioHub server did not repond correctly, terminate process and exit the program.
        if hubonline is False:
            print "ioHub could not be contacted, exiting...."
            try:
                self._server_process.terminate()
            except Exception as e:
                raise ioHubConnectionException(e)
            finally:
                sys.exit(1)

        # save ioHub ProcessID to file so next time it is started, it can be checked and killed if necessary

        try:
            iopFile= open(iopFileName,'w')
            iopFile.write("ioHub PID: "+str(Computer.ioHubServerProcessID))
            iopFile.flush()
            iopFile.close()
        except:
            ioHub.printExceptionDetailsToStdErr()


        # create a local 'thin' representation of the registered ioHub devices,
        # allowing such things as device level event access (if supported) 
        # and transparent IPC calls of public device methods and return value access.
        # Devices are available as hub.devices.[device_name] , where device_name
        # is the name given to the device in the ioHub .yaml config file to be access;
        # i.e. hub.devices.ExperimentPCkeyboard would access the experiment PC keyboard
        # device if the default name was being used.
        self._createDeviceList()
                    
    def _get_maxsize(self, maxsize):
        """
        Used by _startServer pipe reader code.
        """
        if maxsize is None:
            maxsize = 1024
        elif maxsize < 1:
            maxsize = 1
        return maxsize

    def _serverStdOutHasData(self, maxsize=256):
        """
        Used by _startServer pipe reader code. Allows for async check for data on pipe in windows.
        """
        #  >> WIN32_ONLY
        import msvcrt
        from win32pipe import PeekNamedPipe

        maxsize = self._get_maxsize(maxsize)
        conn=self._server_process.stdout

        if conn is None:
            return False
        try:
            x = msvcrt.get_osfhandle(conn.fileno())
            (read, nAvail, nMessage) = PeekNamedPipe(x, 0)
            if maxsize < nAvail:
                nAvail = maxsize
            if nAvail > 0:
                return True
        # << WIN32_ONLY
        except Exception as e:
            raise ioHubConnectionException(e)

    def _readServerStdOutLine(self):
        """
        Used by _startServer pipe reader code. Reads a line from the ioHub server stdout. This is blocking.
        """
        for line in iter(self._server_process.stdout.readline, ''):
            yield line

    def _calculateClientServerTimeOffset(self, sampleSize=100):
        """
        Calculates 'sampleSize' experimentTime and ioHub Server time process offsets by calling currentSec locally
        and via IPC to the ioHub server process repeatedly, as ewell as calculating the round trip time it took to get the server process time in each case.
        Puts the 'sampleSize' calculates in a 2D numpy array, index [i][0] = server_time - local_time offset, index [i][1]
        = local_time after call to server_time - local_time before call to server_time.

        In Windows, since direct QPC implementation is used, offset should == delay to within 100 usec or so.
        """
        results=N.zeros((sampleSize,2),dtype='f4')
        for i in xrange(sampleSize):     # make multiple calles to local and ioHub times to calculate 'offset' and 'delay' in calling the ioHub server time
            tc=Computer.currentSec()*1000.0   # get the local time 1
            ts=self.currentSec()*1000.0        # get the ioHub server time (this results in a RPC call and response from the server)
            tc2=Computer.currentSec()*1000.0   # get local time 2, to calculate e2e delay for the ioHub server time call
            results[i][0]=ts-tc          # calculate time difference between returned iohub server time, and 1 read local time (offset)
            results[i][1]=tc2-tc         # calculate delay / duration it took to make the call to the ioHub server and get reply
            time.sleep(0.001)            # sleep for a little before going next loop
        #print N.min(results,axis=0) ,N.max(results,axis=0) , N.average(results,axis=0), N.std(results,axis=0)
        return results

    def _createDeviceList(self):
        """
        Populate the devices attribute object with the registered devices of the ioHub. Each ioHub device becomes an attribute
        of the devices instance, with the attribute name == the name give the device in the ioHub configuration file.
        Each device in allows access to the pupic method interface of the device via transparent IPC calls to the ioHub server process
        from the expriment process.
        """
        # get the list of devices registered with the ioHub
        deviceList=self._getDeviceList()

        # create an experiment process side device object to allow access to the public interface of the
        # ioHub device via transparent IPC.
        for name,device_class in deviceList:
            d=ioHubDeviceView(self,name,device_class)
            self.devices.__dict__[name]=d
            self.deviceByLabel[name]=d

    # UDP communication with ioHost
    def sendToHubServer(self,ioHubMessage):
        """
        General purpose message sending routine,  used to send a message from the PsychoPy Process
        to the ioHub Process, and then wait for the reply from the ioHub Process before returning.

        The ioHub Server accepts data send either encoded using the [msgpack](https://github.com/msgpack/msgpack-python)
        library or json encoded using the [ujson](https://github.com/esnme/ultrajson) library
        ( it is **fast** ).

        Which encoding type is used is specified in the ioHub configuration file by the ipcCoder: parameter.
        By default *msgpack* is selected, as it seems to be as fast as ujson, but it also compresses
        event data by up to 40 - 50% for transmission.

        All messages sent to the ioHub (a.k.a the ioHubMessage param) have the following simple format:

        (msg_type, [callable_name_for_IPC], ( [optional_list_of_args], {optional_dict_of_kw_args} ) )

        The currently supported message types are:

        #. RPC
        #. GET_EVENTS
        #. EXP_DEVICE

        Every request to the ioHub Server Process is sent a response / reply back, even if it
        is just to indicate the request was receive and if it was processed successfully or not. All
        responses from the ioHub server are in the form:

        (response_type, *response_values)

        where *response_values is a list of objects representing the response payload.

        The current ioHub response types are:

        #. RPC_RESULT
        #. GET_EVENTS_RESULT
        #. DEV_RPC_RESULT
        #. GET_DEV_LIST_RESULT
        #. GET_DEV_INTERFACE
        #. IOHUB_ERROR
        #. RPC_ERROR

        The ioHubConnection currently blocks until the request is fulfilled and and a response is
        received from the ioHub server.

        TODO: An aysnc. version could be added if desired. Instead of using callbacks, I prefer the
        idea of the client sending a request and getting a request ticket # back from the ioHub server
        right away, indicating that the job has been submitted for processing. The ioHubConnection can
        then ask the ioHub Server for the status of the job ticket based on ticket number.
        When the ticket number result is ready, it is sent back as the reply to the status request.
        This **aysnc. mode will be necessary** when the worker process is added to the ioHub framework
        to handle long running job requests from the PsychoPy process; for example to load an image
        into a shared memory space, perform long running computations, etc.

        Args:
            messageList (tuple): ioHub Server Message to send.

        Return (object): the message response from the ioHub Server process.
        """

        # send request to host, return is # bytes sent.
        bytes_sent=self.udp_client.sendTo(ioHubMessage)

        # wait for response from ioHub server, return is result ( decoded already ), and Hub address (ip4,port).
        result,address=self.udp_client.receive()

        # store result received in an address based dictionary (incase we ever support multiple ioHub Servers)
        ioHubConnection._addResponseToHistory(result,bytes_sent,address)

        # check if the reply is an error or not. If it is, raise the error.
        errorReply=self._isErrorReply(result)
        if errorReply:
            raise errorReply

        #Otherwise return the result
        return result


    def updateGlobalHubTimeOffset(self,offset):
        r=self.sendToHubServer(('RPC','updateGlobalTimeOffset',(offset,)))
        print 'updateGlobalHubTimeOffset client got: ',r,' : ',r[2]
        return r[2]

    @classmethod
    def _addResponseToHistory(cls,result,bytes_sent,address):
        """
        Adds a response from the ioHub to an ip:port based dictionary. Not used right now, but may be useful if we ever support
        a client connecting to > 1 ioHub.
        """
        address=str(address)
        if address in cls._replyDictionary:
            cls._replyDictionary[address].append((result,bytes_sent))
        else:
            cls._replyDictionary[address]=deque(maxlen=128)
            cls._replyDictionary[address].append((result,bytes_sent))



    def _sendExperimentInfo(self,experimentInfoDict):
        """
        Sends the experiment info from the experiment config file to the ioHub Server, which passes it to the ioDataStore,
        determines if the experiment already exists in the experiment file based on 'experiment_code', and returns a new
        or existing experiment ID based on that criteria.
        """
        fieldOrder=(('experiment_id',0), ('code','') , ('title','') , ('description','')  , ('version','') , ('total_sessions_to_run',0))
        values=[]
        for key,defaultValue in fieldOrder:
            if key in experimentInfoDict:
                values.append(experimentInfoDict[key])
            else:
                values.append(defaultValue)

        r=self.sendToHubServer(('RPC','setExperimentInfo',(values,)))
        self.experimentID=r[2]
        return r[2]

    def _sendSessionInfo(self,sessionInfoDict):
        """
        Sends the experiment session info from the experiment config file and the values entered into the session dialog
        to the ioHub Server, which passes it to the ioDataStore, determines if the experiment already exists in the
        experiment file based on 'experiment_code', and returns a new or existing experiment ID based on that criteria.
        """
        if self.experimentID is None:
            raise ioHubConnectionException("Experiment ID must be set by calling _sendExperimentInfo before calling _sendSessionInfo.")
        if 'code' not in sessionInfoDict:
            raise ioHubConnectionException("Code must be provided in sessionInfoDict ( StringCol(8) ).")
        if 'name' not in sessionInfoDict:
            sessionInfoDict['name']=''
        if 'comments' not in sessionInfoDict:
            sessionInfoDict['comments']=''
        if 'user_variables' not in sessionInfoDict:
            sessionInfoDict['user_variables']={}

        import ujson
        sessionInfoDict['user_variables']=ujson.encode(sessionInfoDict['user_variables'])

        r=self.sendToHubServer(('RPC','createExperimentSessionEntry',(sessionInfoDict,)))

        self.experimentSessionID=r[2]
        return r[2]

    def initializeExperimentConditionVariableTable(self, conditionVariablesProvider):
        r=self.sendToHubServer(('RPC','initializeExperimentConditionVariableTable',(self.experimentID,self.experimentSessionID,conditionVariablesProvider._numpyConditionVariableDescriptor)))
        return r[2]

    def addRowToExperimentConditionVariableTable(self,data):
        r=self.sendToHubServer(('RPC','addRowToExperimentConditionVariableTable',(self.experimentSessionID,data)))
        return r[2]

    def getDevice(self,deviceName):
        """
        Returns the ioHubDeviceView that has a matching name (based on the 
        device : name property specified in the ioHub_config.yaml for the 
        experiment). If no device is found matching the name, None is returned.

        i.e.

            keyboard = self.getDevice('kb')
            kb_events= keyboard.getEvent()
            
        This is the same as using the 'natural naming' support in the 
        ioHubExperimentRuntime class, i.e:
            
            keyboard = self.devices.kb
            kb_events= keyboard.getEvent()
        
        Args:
            deviceName (str): Name given to the ioHub Device to be returned
        Returns:
            device (ioHubDeviceView) : the experimentRuntime represention
                    for the device that matches the name provided.
        """
        return self.deviceByLabel.get(deviceName,None)
        
    def _getEvents(self):
        """
        Sends a request to the ioHub Server for any new device events from the global server event buffer.
        The events are returned and the global ioHub server event buffer is cleared.

        Args: None
        Return(tuple): list of events, or empty list if no events have occurred since last call
              to getEvents() or clearEvents(). Each event in the list is a tuple containing the ordered
              attributes of the event constructor.
        """
        r = self.sendToHubServer(('GET_EVENTS',))
        return r[1]

    def getEvents(self,deviceLabel=None,asType='dict'):
        """
        Retrieve any events that have been collected by the ioHub server from monitored devices
        since the last call to getEvents() or since the last call to clearEvents().

        By default all events for all monitored devices are returned, with each event being
        represented as a dictionary of event attributes. When events are retrieved from an event buffer,
        they are removed from the buffer as well.

        Args:
            deviceLabel (str): optional : if specified, indicates to only retrieve events for
                         the device with the associated label name. None (default) returns
                         all device events.
            asType (str): optional : indicated how events should be represented when they are returned.
                         Default: 'dict'
                Events are sent from the ioHub Process as lists of ordered attributes. This is the most
                efficient for data transmission, but not for readability.

                If you do want events to be kept in list form, set asType = 'list'.

                Setting asType = 'dict' (the default) converts the events lists to event dictionaries.
                This process is quite fast so the small conversion time is usually worth it given the
                huge benefit in usability within your program.

                Setting asType = 'object' converts the events to their ioHub DeviceEvent class form.
                This can take a bit of time if the event list is long and currently there is not much
                benefit in doing so vs. treating events as dictionaries. This may change in
                the future. For now, it is suggested that the default, asType='dict' setting be kept.

        Return (tuple): returns a list of event objects, where the object type is defined by the
                'asType' parameter.
        """

        r=None
        if deviceLabel is None:
            events=self._getEvents()
            if events is None:
                r=self.allEvents    
            else:
                self.allEvents.extend(events)
                r=self.allEvents
            self.allEvents=[]
        else:
            d=self.deviceByLabel[deviceLabel]
            r=d.getEvents()
  
        if r:
            if asType == 'list':
                return r
            else:
                conversionMethod=None
                if asType == 'dict':
                    conversionMethod=self._eventListToDict
                elif asType == 'object':
                    conversionMethod=self._eventListToObject
                
                if conversionMethod:                    
                    events=[]
                    for el in r:
                        events.append(conversionMethod(el))
                    return events
                
                return r

    def clearEvents(self,deviceLabel=None):
        """
        Clears all events from the global event buffer, or if deviceLabel is not None,
        clears the events only from a specific device event buffer.
        When the global event buffer is cleared, device level event buffers are not effected.

        Args:
            devicelabel (str): name of the device that should have it's event buffer cleared.
                         If None (the default), the device wide event buffer is cleared
                         and device level event buffers are not changed.
        Return: None
        """
        if deviceLabel is None or deviceLabel.lower() == 'all':
            self.sendToHubServer(('RPC','clearEventBuffer'))
            self.allEvents=[]
            if deviceLabel and deviceLabel.lower() == 'all':
                [self.deviceByLabel[label].clearEvents() for label in self.deviceByLabel]
            return True
        else:
            d=self.deviceByLabel.get(deviceLabel,None)
            if d:
                d.clearEvents()
                return True
            return False

    def delay(self,delay,checkHubInterval=0.01):
        """
        Pause the experiment execution for msec.usec interval, while checking the ioHub for
        any new events and retrieving them every 'checkHubInterval' msec during the delay. Any events
        that are gathered during the delay period will be handed to the experiment the next time
        self.getEvents() is called, unless self.clearEvents() beforehand.

        It is important to allow the PyschoPy Process to periodically either call self.getEvents() events
        during long delaying in program execution so that a) the event queues
        to not reach the specified limits and start descarding old events when 
        new events arrive, and b) so that a very large build up of events does
        not occur on the ioHub Process, that then takes multiple UDP packets
        to transmit to the experiment. This will slow event retrieval down
        unnecessarily. If you are using delay, may as well occationally have the
        experiment process occationally grab any new events from
        the ioHub process during it.

        Also keep in mind that calling self.clearEvents() after any long delays
        between calls to self.getEvents() or self.clearEvents() will clear
        events from the ioHub server so they are not uncessarily  sent to
        the experiment process if you do not need them (they are still being
        stored in the ioDataStore assuming the Device has event reporting
        enabled of course). 
        
        Args:
            delay (float/double): the sec.msec_usec period that the PsychoPy Process should wait
                              before returning from the function call.
            checkHubInterval (float/double): the sec.msec_usec interval after which any ioHub
                              events will be retrieved (by calling self.getEvents) and stored
                              in a local buffer. This is repeated every checkHubInterval sec.msec_usec until
                              the method completes. Default is every 0.01 sec ( 10.0 msec ).

        Return(float/double): actual duration of delay in sec.msec_usec format.
        """
        stime=Computer.currentTime()
        targetEndTime=stime+delay

        if checkHubInterval < 0:
            checkHubInterval=0
        
        if checkHubInterval > 0:
            remainingSec=targetEndTime-Computer.currentTime()
            while remainingSec > 0.001:
                if remainingSec < checkHubInterval+0.001:
                    time.sleep(remainingSec)
                else:
                    time.sleep(checkHubInterval)
                    events=self.getEvents()
                    if events:
                        self.allEvents.extend(events)
                    pumpLocalMessageQueue()
                
                remainingSec=targetEndTime-Computer.currentTime()
            
            while (targetEndTime-Computer.currentTime())>0.0:
                pass
        else:
            time.sleep(delay-0.001)
            while (targetEndTime-Computer.currentTime())>0.0:
                pass
                
        return Computer.currentTime()-stime

    @staticmethod
    def _eventListToObject(eventValueList):
        """
        Convert an ioHub event that is current represented as an orderded list of values, and return the correct
        ioHub.devices.DeviceEvent subclass for the given event type.
        """
        eclass=EventConstants.EVENT_CLASSES[eventValueList[3]]
        combo = zip(eclass.CLASS_ATTRIBUTE_NAMES,eventValueList)
        kwargs = dict(combo)
        return eclass(**kwargs)

    @staticmethod
    def _eventListToDict(eventValueList):
        """
        Convert an ioHub event that is current represented as an ordered list of values, and return the event as a
        dictionary of attribute name, attribute values for the object.
        """
        try:
            if isinstance(eventValueList,dict):
                return eventValueList
            eclass=EventConstants.EVENT_CLASSES[eventValueList[DeviceEvent.EVENT_TYPE_ID_INDEX]]
            combo = zip(eclass.CLASS_ATTRIBUTE_NAMES,eventValueList)
            return dict(combo)
        except:
            print '---------------'
            print "ERROR: eventValueList: "+str(eventValueList)
            print '---------------'



    def sendEvents(self,events):
        """
        Send 1 - N ExperimentDevice Events (currently MessageEvents are supported) to the ioHub Process
        for storage. Each object in the events list must be a tuple containing the ordered
        attributes of the event constructor.

        Args:
            events(tuple): list of ExperimentEvents
        Return (int): the number of events that the ioHub Server process successfully parsed and saved.
        """
        eventList=[]
        for e in events:
            eventList.append(e.asList())
        r=self.sendToHubServer(('EXP_DEVICE','EVENT_TX',eventList))
        return r

    def sendMessageEvent(self,text,prefix='',offset=0.0,sec_time=None):
        """
        Create and send a MessageEvent to the ioHub Server Process for storage
        with the rest of the event data.

        Args:
          text (str): The text message for the message event. Can be up to 128 characters in length.
          prefix (str): A 0 - 3 character prefix for the message that can be used to sort or group
                        messages by 'types'
          offset (float): The usec offset to apply to the time stamp of the message event.
                          If you send the event before or after the time the event actually occurred,
                          and you know what the offset value is, you can provide it here and it
                          will be applied to the ioHub time stamp for the event.
          usec_time (int/long): Since (at least on Windows currently) if you use the ioHub timers,
                                the time-base of the experiment process is identical to that of the ioHub
                                server process, then you can specific the ioHub time (in usecs) for
                                experiment events right in the experiment process itself.

        Return (bool): True
        """
        self.sendToHubServer(('EXP_DEVICE','EVENT_TX',[MessageEvent._createAsList(text,prefix=prefix,msg_offset=offset,sec_time=sec_time),]))
        return True

    def sendMessages(self,messageList):
        """
        Same as the sendMessage method, but accepts a list of lists of message arguments,
        so you can have N messages created and sent at once.

        Args:
            messageList(tuple): list of lists, where each inner list represents a MessageEvent in list form
                           (i.e as an ordered list of event attribute value as would be passed to the
                            MessageEvent constructor)
        Return (bool): True
        """
        msgEvents=[]
        for msg in messageList:
            msgEvents.append(MessageEvent._createAsList(*msg))
        self.sendToHubServer(('EXP_DEVICE','EVENT_TX',msgEvents))
        return True

    # client utility methods.
    def _getDeviceList(self):
        r=self.sendToHubServer(('EXP_DEVICE','GET_DEV_LIST'))
        return r[2]

    def shutdown(self):
        self._shutDownServer()
        
    def _shutDownServer(self):
        """

        :return:
        """
        try:
            self.udp_client.sendTo(('RPC','shutDown'))
            self.udp_client.close()
            if Computer.ioHubServerProcess:
                r=Computer.ioHubServerProcess.wait(timeout=5)
                print 'ioHub Server Process Completed With Code: ',r
        except psutil.TimeoutExpired:
            print "Warning: TimeoutExpired, Killing ioHub Server process."
            Computer.ioHubServerProcess.kill()
        except Exception:
            print "Warning: Unhandled Exception. Killing ioHub Server process."
            if Computer.ioHubServerProcess:
                Computer.ioHubServerProcess.kill()
            ioHub.printExceptionDetailsToStdErr()
        finally:
            self._server_process=None
            Computer.ioHubServerProcessID=None
            Computer.ioHubServerProcess=None
        return True

    def currentSec(self):
        """
        Returns the sec.msec-usec time retrieved from the ioHub Server process. This method sends a message to
        the ioHub Process asking for the currentSec time on that process, and returns the result.

        Therefore there will be a small delay in getting the current ioHub Process time, and this means
        current_ioHub_secs = self.currentSec()-DELAY, where delay is the time from when the
        ioHub Server Process read the currentSec time to when the PsychoPy process received the reply.

        **Note:** On Windows, both the PsychoPy and ioHub Processes use the same time base
               if you call one of the ioHub Experiment Runtime time methods, so there is **no need**
               to call this method to get the current ioHub Process time.

               It will be more accurate to call one of the following time methods, which gives you
               the current ioHub Process and PsychoPy Process time, as they are the same:

               * ioHub.highPrecisionTimer() : returns sec.msec-usec time
               * ioHub.devices.Computer.getSec() : returns sec.msec-usec time


               If running your experiment within the run() method of a class extended from
               ioHub.experiment.ioHubExperimentRuntime, you can also use:

               * self.currentSec()

        Args: None
        Return (float/double): The ioHub Process sec.msec-usec time when the request was processed
                               by the ioHub Server Process.
        """
        r=self.sendToHubServer(('RPC','currentSec'))
        return r[2]

    def enableHighPriority(self,disable_gc=False):
        """        
        Sets the priority of the ioHub Process to high priority
        and optionally (default is False) disable the python GC. This is
        useful for the duration of a trial, for example, where you enable at
        start of trial and disable at end of trial. Improves Windows
        sloppiness greatly in general.

        Args:
            disable_gc(bool): True = Turn of the Python Garbage Collector. 
                              False = Leave the Garbage Collector running.
                              Default: True
        """
        self.sendToHubServer(('RPC','enableHighPriority',(disable_gc,)))
        
    def disableHighPriority(self):
        """
        Sets the priority of the ioHub Process back to normal priority
        and enables the python GC. In general you would call 
        enableHighPriority() at start of trial and call 
        disableHighPriority() at end of trial.

        Return: None
        """
        self.sendToHubServer(('RPC','disableHighPriority'))
        
    def getProcessAffinity(self):
        """
        Returns the ioHub Process Affinity setting, as a list of 'processor' id's
        (from 0 to getSystemProcessorCount()-1) that the ioHub Process is able to
        run on.

        For example, on a 2 core CPU with hyper-threading, the possible 'processor' list would be
        [0,1,2,3], and by default the ioHub Process can run on any of these 'processors', so:


        ioHubCPUs=self.getProcessAffinity()
        print ioHubCPUs

        >> [0,1,2,3]
        """
        r=self.sendToHubServer(('RPC','getProcessAffinity'))
        return r[2]

    def setProcessAffinity(self, processorList):
        """
        Sets the ioHub Process Affinity based on processorList, a list of 'processor' id's
        (from 0 to getSystemProcessorCount()-1) that the ioHub Process is able to run on.

        For example, on a 2 core CPU with hyper-threading, the possible 'processor' list would be [0,1,2,3],
        and by default ioHub server processes can run on any of these 'processors'. To set the ioHub Process to
        only run on core 2 of the CPU, you would call:

        self.setProcessAffinity([2,3])

        # check the ioHub Process affinities
        ioHubCPUs=self.getProcessAffinity()
        print ioHubCPUs

        >> [2,3]
        """
        r=self.sendToHubServer(('RPC','setProcessAffinity',processorList))
        return r[2]

    def flushIODataStoreFile(self):
        """
        Manually tell the ioDataStore to flush any events it has beuffered for storage from memory to disk."
        """
        r=self.sendToHubServer(('RPC','flushIODataStoreFile'))
        print "flushIODataStoreFile: ",r[2]
        return r[2]

    def _isErrorReply(self,data):
        """

        """
        if ioHub.isIterable(data) and len(data)>0:
            if ioHub.isIterable(data[0]):
                return False
            else:
                if (type(data[0]) in (str,unicode)) and data[0].find('ERROR')>=0:
                    return ioHubConnectionException(data)
                return False
        else:
            raise ioHubConnectionException('Response from ioHub should always be iterable and have a length > 0')

    def __del__(self):
        try:
            self._shutDownServer()
        except:
            pass