#!/usr/bin/env python
#pylint: disable-msg=W0613, W6501
"""
_Tier0FeederPoller_

The actual work done by the Tier0Feeder component

Checks for new data
Populates RunConfig for new runs
Setup subscriptions for new runs/streams
Assigns new data to the correct subscriptions

"""
import os
import logging
import threading
import subprocess

from WMCore.WorkerThreads.BaseWorkerThread import BaseWorkerThread
from WMCore.DAOFactory import DAOFactory
from WMCore.Database.DBFactory import DBFactory
from WMCore.WMException import WMException
from WMCore.Configuration import loadConfigurationFile
from WMCore.Services.RequestDB.RequestDBWriter import RequestDBWriter

from T0.RunConfig import RunConfigAPI
from T0.RunLumiCloseout import RunLumiCloseoutAPI
from T0.ConditionUpload import ConditionUploadAPI


class Tier0FeederPoller(BaseWorkerThread):

    def __init__(self, config):
        """
        _init_

        """
        BaseWorkerThread.__init__(self)

        myThread = threading.currentThread()

        self.daoFactory = DAOFactory(package = "T0.WMBS",
                                     logger = logging,
                                     dbinterface = myThread.dbi)

        self.tier0ConfigFile = config.Tier0Feeder.tier0ConfigFile
        self.specDirectory = config.Tier0Feeder.specDirectory
        self.dropboxuser = getattr(config.Tier0Feeder, "dropboxuser", None)
        self.dropboxpass = getattr(config.Tier0Feeder, "dropboxpass", None)

        self.transferSystemBaseDir = getattr(config.Tier0Feeder, "transferSystemBaseDir", None)
        if self.transferSystemBaseDir != None:
            if not os.path.exists(self.transferSystemBaseDir):
                self.transferSystemBaseDir = None

        self.dqmUploadProxy = getattr(config.Tier0Feeder, "dqmUploadProxy", None)
        self.serviceProxy = getattr(config.Tier0Feeder, "serviceProxy", None)

        self.localRequestCouchDB = RequestDBWriter(config.AnalyticsDataCollector.localT0RequestDBURL, 
                                                   couchapp = config.AnalyticsDataCollector.RequestCouchApp)

        hltConfConnectUrl = config.HLTConfDatabase.connectUrl
        dbFactoryHltConf = DBFactory(logging, dburl = hltConfConnectUrl, options = {})
        dbInterfaceHltConf = dbFactoryHltConf.connect()
        daoFactoryHltConf = DAOFactory(package = "T0.WMBS",
                                       logger = logging,
                                       dbinterface = dbInterfaceHltConf)
        self.getHLTConfigDAO = daoFactoryHltConf(classname = "RunConfig.GetHLTConfig")

        storageManagerConnectUrl = config.StorageManagerDatabase.connectUrl
        dbFactoryStorageManager = DBFactory(logging, dburl = storageManagerConnectUrl, options = {})
        self.dbInterfaceStorageManager = dbFactoryStorageManager.connect()

        self.getExpressReadyRunsDAO = None
        if hasattr(config, "PopConLogDatabase"):
            popConLogConnectUrl = getattr(config.PopConLogDatabase, "connectUrl", None)
            if popConLogConnectUrl != None:
                dbFactoryPopConLog = DBFactory(logging, dburl = popConLogConnectUrl, options = {})
                dbInterfacePopConLog = dbFactoryPopConLog.connect()
                daoFactoryPopConLog = DAOFactory(package = "T0.WMBS",
                                                 logger = logging,
                                                 dbinterface = dbInterfacePopConLog)
                self.getExpressReadyRunsDAO = daoFactoryPopConLog(classname = "Tier0Feeder.GetExpressReadyRuns")

        self.haveT0DataSvc = False
        if hasattr(config, "T0DataSvcDatabase"):
            t0datasvcConnectUrl = getattr(config.T0DataSvcDatabase, "connectUrl", None)
            if t0datasvcConnectUrl != None:
                self.haveT0DataSvc = True
                dbFactoryT0DataSvc = DBFactory(logging, dburl = t0datasvcConnectUrl, options = {})
                dbInterfaceT0DataSvc = dbFactoryT0DataSvc.connect()
                self.daoFactoryT0DataSvc = DAOFactory(package = "T0.WMBS",
                                                      logger = logging,
                                                      dbinterface = dbInterfaceT0DataSvc)

        return

    def algorithm(self, parameters = None):
        """
        _algorithm_

        """
        logging.debug("Running Tier0Feeder algorithm...")
        myThread = threading.currentThread()

        findNewRunsDAO = self.daoFactory(classname = "Tier0Feeder.FindNewRuns")
        findNewRunStreamsDAO = self.daoFactory(classname = "Tier0Feeder.FindNewRunStreams")
        findNewExpressRunsDAO = self.daoFactory(classname = "Tier0Feeder.FindNewExpressRuns")
        releaseExpressDAO = self.daoFactory(classname = "Tier0Feeder.ReleaseExpress")
        feedStreamersDAO = self.daoFactory(classname = "Tier0Feeder.FeedStreamers")
        markWorkflowsInjectedDAO = self.daoFactory(classname = "Tier0Feeder.MarkWorkflowsInjected")

        tier0Config = None
        try:
            tier0Config = loadConfigurationFile(self.tier0ConfigFile)
        except:
            # usually happens when there are syntax errors in the configuration
            logging.exception("Cannot load Tier0 configuration file, not configuring new runs and run/streams")

        # only configure new runs and run/streams if we have a valid Tier0 configuration
        if tier0Config != None:

            #
            # find new runs, setup global run settings and stream/dataset/trigger mapping
            #
            runHltkeys = findNewRunsDAO.execute(transaction = False)
            for run, hltkey in sorted(runHltkeys.items()):

                hltConfig = None

                # local runs have no hltkey and are configured differently
                if hltkey != None:

                    # retrieve HLT configuration and make sure it's usable
                    try:
                        hltConfig = self.getHLTConfigDAO.execute(hltkey, transaction = False)
                        if hltConfig['process'] == None or len(hltConfig['mapping']) == 0:
                            raise RuntimeError("HLTConfDB query returned no process or mapping")
                    except:
                        logging.exception("Can't retrieve hltkey %s for run %d" % (hltkey, run))
                        continue

                try:
                    RunConfigAPI.configureRun(tier0Config, run, hltConfig)
                except:
                    logging.exception("Can't configure for run %d" % (run))

            #
            # find unconfigured run/stream with data
            # populate RunConfig, setup workflows/filesets/subscriptions
            # 
            runStreams = findNewRunStreamsDAO.execute(transaction = False)
            for run in sorted(runStreams.keys()):
                for stream in sorted(runStreams[run]):
                    try:
                        RunConfigAPI.configureRunStream(tier0Config,
                                                        run, stream,
                                                        self.specDirectory,
                                                        self.dqmUploadProxy)
                    except:
                        logging.exception("Can't configure for run %d and stream %s" % (run, stream))

        #
        # stop and close runs based on RunSummary and StorageManager records
        #
        RunLumiCloseoutAPI.stopRuns(self.dbInterfaceStorageManager)
        RunLumiCloseoutAPI.closeRuns(self.dbInterfaceStorageManager)

        #
        # release runs for Express
        #
        runs = findNewExpressRunsDAO.execute(transaction = False)

        if len(runs) > 0:

            binds = []
            for run in runs:
                binds.append( { 'RUN' : run } )

            if self.getExpressReadyRunsDAO != None:
                runs = self.getExpressReadyRunsDAO.execute(binds = binds, transaction = False)

            if len(runs) > 0:

                binds = []
                for run in runs:
                    binds.append( { 'RUN' : run } )

                releaseExpressDAO.execute(binds = binds, transaction = False)

        #
        # release runs for PromptReco
        #
        RunConfigAPI.releasePromptReco(tier0Config,
                                       self.specDirectory,
                                       self.dqmUploadProxy)

        #
        # insert express and reco configs into Tier0 Data Service
        #
        if self.haveT0DataSvc:
            self.updateRunStreamDoneT0DataSvc()
            self.updateExpressConfigsT0DataSvc()
            self.updateRecoConfigsT0DataSvc()
            self.updateRecoReleaseConfigsT0DataSvc()
            self.lockDatasetsT0DataSvc()

        #
        # mark express and repack workflows as injected if certain conditions are met
        # (we don't do it immediately to prevent the TaskArchiver from cleaning up too early)
        #
        markWorkflowsInjectedDAO.execute(self.transferSystemBaseDir != None,
                                         transaction = False)

        #
        # close stream/lumis for run/streams that are active (fileset exists and open)
        #
        RunLumiCloseoutAPI.closeLumiSections(self.dbInterfaceStorageManager)

        #
        # feed new data into exisiting filesets
        #
        try:
            myThread.transaction.begin()
            feedStreamersDAO.execute(conn = myThread.transaction.conn, transaction = True)
        except:
            logging.exception("Can't feed data, bailing out...")
            raise
        else:
            myThread.transaction.commit()

        #
        # run ended and run/stream fileset open
        #    => check for complete lumi_closed record, all lumis finally closed and all data feed
        #          => if all conditions satisfied, close the run/stream fileset
        #
        RunLumiCloseoutAPI.closeRunStreamFilesets()

        #
        # check and delete active split lumis
        #
        RunLumiCloseoutAPI.checkActiveSplitLumis()

        #
        # insert workflows into CouchDB for monitoring
        #
        self.feedCouchMonitoring()

        #
        # Update Couch when Repack and Express have closed input filesets (analog to old T0 closeout)
        #
        self.closeOutRealTimeWorkflows()

        #
        # send repacked notifications to StorageManager
        #
        if self.transferSystemBaseDir != None:
            self.notifyStorageManager()

        #
        # upload PCL conditions to DropBox
        #
        ConditionUploadAPI.uploadConditions(self.dropboxuser, self.dropboxpass, self.serviceProxy)

        return

    def feedCouchMonitoring(self):
        """
        _feedCouchMonitoring_

        check for workflows that haven't been uploaded to Couch for monitoring yet

        """
        getStreamerWorkflowsForMonitoringDAO = self.daoFactory(classname = "Tier0Feeder.GetStreamerWorkflowsForMonitoring")
        getPromptRecoWorkflowsForMonitoringDAO = self.daoFactory(classname = "Tier0Feeder.GetPromptRecoWorkflowsForMonitoring")
        markTrackedWorkflowMonitoringDAO = self.daoFactory(classname = "Tier0Feeder.MarkTrackedWorkflowMonitoring")
        workflows = getStreamerWorkflowsForMonitoringDAO.execute()
        workflows += getPromptRecoWorkflowsForMonitoringDAO.execute()

        if len(workflows) == 0:
            logging.debug("No workflows to publish to couch monitoring, doing nothing")

        if workflows:
            logging.debug(" Going to publish %d workflows" % len(workflows))
            for (workflowId, run, workflowName) in workflows:
                logging.info(" Publishing workflow %s to monitoring" % workflowName)
                #TODO: add more information about workflow if there need to be kept longer than 
                # workflow life cycle.
                doc = {}
                doc["RequestName"] =   workflowName
                doc["Run"]      =   run
                response = self.localRequestCouchDB.insertGenericRequest(doc)
                if response == "OK" or "EXISTS":
                    logging.info(" Successfully uploaded request %s" % workflowName)
                    # Here we have to trust the insert, if it doesn't happen will be easy to spot on the logs
                    markTrackedWorkflowMonitoringDAO.execute(workflowId)

        return

    def closeOutRealTimeWorkflows(self):
        """
        _closeOutRealTimeWorkflows_

        Updates couch with the closeout status of Repack and Express
        PromptReco should be closed out automatically

        """
        getNotClosedOutWorkflowsDAO = self.daoFactory(classname = "Tier0Feeder.GetNotClosedOutWorkflows")
        workflows = getNotClosedOutWorkflowsDAO.execute()

        if len(workflows) == 0:
            logging.debug("No workflows to publish to couch monitoring, doing nothing")

        if workflows:
            for workflow in workflows:
                (workflowId, filesetId, filesetOpen, workflowName) = workflow
                # find returns -1 if the string is not found
                if workflowName.find('PromptReco') >= 0:
                    logging.debug("Closing out instantaneously PromptReco Workflow %s" % workflowName)
                    self.updateClosedState(workflowName, workflowId)
                else :
                    # Check if fileset (which you already know) is closed or not
                    # FIXME: No better way to do it? what comes from the DAO is a string, casting bool or int doesn't help much.
                    # Works like that :
                    if filesetOpen == '0':
                        self.updateClosedState(workflowName, workflowId)

        return

    def updateClosedState(self, workflowName, workflowId):
        """
        _updateClosedState_

        Mark a workflow as Closed

        """
        markCloseoutWorkflowMonitoringDAO = self.daoFactory(classname = "Tier0Feeder.MarkCloseoutWorkflowMonitoring")

        response = self.localRequestCouchDB.updateRequestStatus(workflowName, 'Closed')

        if response == "OK" or "EXISTS":
            logging.debug("Successfully closed workflow %s" % workflowName)
            markCloseoutWorkflowMonitoringDAO.execute(workflowId)

        return

    def notifyStorageManager(self):
        """
        _notifyStorageManager_

        Find all finished streamers for closed all run/stream
        Send the notification message to StorageManager
        Update the streamer status to finished (deleted = 1)

        """
        getFinishedStreamersDAO = self.daoFactory(classname = "SMNotification.GetFinishedStreamers")
        markStreamersFinishedDAO = self.daoFactory(classname = "SMNotification.MarkStreamersFinished")

        allFinishedStreamers = getFinishedStreamersDAO.execute(transaction = False)

        chunkSize = 50
        for finishedStreamers in [ allFinishedStreamers[i:i+chunkSize] for i in range(0, len(allFinishedStreamers), chunkSize) ]:

            streamers = []
            filenameParams = ""

            for (id, lfn) in finishedStreamers:
                streamers.append(id)
                filenameParams += "-FILENAME %s " % os.path.basename(lfn)

            logging.debug("Notifying transfer system about processed streamers")
            p = subprocess.Popen("/bin/bash", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = p.communicate("""
            export T0_BASE_DIR=%s
            export T0ROOT=${T0_BASE_DIR}/T0
            export CONFIG=${T0_BASE_DIR}/Config/TransferSystem_CERN.cfg
 
            export PERL5LIB=${T0ROOT}/perl_lib

            unset LANGUAGE
            unset LC_ALL
            unset LC_CTYPE
            export LANG=C

            ${T0ROOT}/operations/sendRepackedStatus.pl --config $CONFIG %s
            """ % (self.transferSystemBaseDir, filenameParams))

            if len(error) > 0:
                logging.error("ERROR: Could not notify transfer system about processed streamers")
                logging.error("ERROR: %s" % error)

            markStreamersFinishedDAO.execute(streamers, transaction = False)

        return

    def updateRunStreamDoneT0DataSvc(self):
        """
        _updateRunStreamDoneT0DataSvc_

        Check if a run/stream workflow (express or repack) is finished and
        cleaned up and push a completion record into the Tier0 Data Service.

        """
        getRunStreamDoneDAO = self.daoFactory(classname = "T0DataSvc.GetRunStreamDone")
        runStreamDone = getRunStreamDoneDAO.execute(transaction = False)

        if len(runStreamDone) > 0:

            binds = []
            for runStream in runStreamDone:
                binds.append( { 'RUN' : runStream['run'],
                                'STREAM' : runStream['stream'] } )

            insertRunStreamDoneDAO = self.daoFactoryT0DataSvc(classname = "T0DataSvc.InsertRunStreamDone")
            insertRunStreamDoneDAO.execute(binds = binds, transaction = False)

            updateRunStreamDoneDAO = self.daoFactory(classname = "T0DataSvc.UpdateRunStreamDone")
            updateRunStreamDoneDAO.execute(binds = binds, transaction = False)

        return

    def updateExpressConfigsT0DataSvc(self):
        """
        _updateExpressConfigsT0DataSvc_

        Check which express_config rows are missing
        in the Tier0 Data Service and insert them,
        also record that fact in t0ast

        """
        getExpressConfigsDAO = self.daoFactory(classname = "T0DataSvc.GetExpressConfigs")
        expressConfigs = getExpressConfigsDAO.execute(transaction = False)

        if len(expressConfigs) > 0:

            bindsInsert = []
            bindsUpdate = []
            for config in expressConfigs:
                bindsInsert.append( { 'RUN' : config['run'],
                                      'STREAM' : config['stream'],
                                      'CMSSW' : config['cmssw'],
                                      'SCRAM_ARCH' : config['scram_arch'],
                                      'RECO_CMSSW' : config['reco_cmssw'],
                                      'RECO_SCRAM_ARCH' : config['reco_scram_arch'],
                                      'ALCA_SKIM' : config['alca_skim'],
                                      'DQM_SEQ' : config['dqm_seq'],
                                      'GLOBAL_TAG' : config['global_tag'][:50],
                                      'SCENARIO' : config['scenario'] } )
                bindsUpdate.append( { 'RUN' : config['run'],
                                      'STREAM' : config['stream'] } )

            insertExpressConfigsDAO = self.daoFactoryT0DataSvc(classname = "T0DataSvc.InsertExpressConfigs")
            insertExpressConfigsDAO.execute(binds = bindsInsert, transaction = False)

            updateExpressConfigsDAO = self.daoFactory(classname = "T0DataSvc.UpdateExpressConfigs")
            updateExpressConfigsDAO.execute(binds = bindsUpdate, transaction = False)

        return

    def updateRecoConfigsT0DataSvc(self):
        """
        _updateRecoConfigsT0DataSvc_

        Check which reco_config rows are missing
        in the Tier0 Data Service and insert them,
        also record that fact in t0ast

        """
        getRecoConfigsDAO = self.daoFactory(classname = "T0DataSvc.GetRecoConfigs")
        recoConfigs = getRecoConfigsDAO.execute(transaction = False)

        if len(recoConfigs) > 0:

            bindsInsert = []
            bindsUpdate = []
            for config in recoConfigs:
                bindsInsert.append( { 'RUN' : config['run'],
                                      'PRIMDS' : config['primds'],
                                      'CMSSW' : config['cmssw'],
                                      'SCRAM_ARCH' : config['scram_arch'],
                                      'ALCA_SKIM' : config['alca_skim'],
                                      'PHYSICS_SKIM' : config['physics_skim'],
                                      'DQM_SEQ' : config['dqm_seq'],
                                      'GLOBAL_TAG' : config['global_tag'][:50],
                                      'SCENARIO' : config['scenario'] } )
                bindsUpdate.append( { 'RUN' : config['run'],
                                      'PRIMDS' : config['primds'] } )

            insertRecoConfigsDAO = self.daoFactoryT0DataSvc(classname = "T0DataSvc.InsertRecoConfigs")
            insertRecoConfigsDAO.execute(binds = bindsInsert, transaction = False)

            updateRecoConfigsDAO = self.daoFactory(classname = "T0DataSvc.UpdateRecoConfigs")
            updateRecoConfigsDAO.execute(binds = bindsUpdate, transaction = False)

        return

    def updateRecoReleaseConfigsT0DataSvc(self):
        """
        _updateRecoReleaseConfigsT0DataSvc_

        Insert information about PromptReco release into the Tier0 Data Service.
        Already aggregate by run, if one primary dataset is released that means
        the whole run is considered released.

        """
        getRecoReleaseConfigsDAO = self.daoFactory(classname = "T0DataSvc.GetRecoReleaseConfigs")
        recoReleaseConfigs = getRecoReleaseConfigsDAO.execute(transaction = False)

        if len(recoReleaseConfigs) > 0:

            bindsInsert = []
            bindsUpdate = []
            for config in recoReleaseConfigs:

                locked = int(config['released'] > 0)

                bindsInsert.append( { 'RUN' : config['run'],
                                      'LOCKED' : locked } )
                bindsUpdate.append( { 'RUN' : config['run'],
                                      'IN_DATASVC' : locked + 1 } )

            insertRecoReleaseConfigsDAO = self.daoFactoryT0DataSvc(classname = "T0DataSvc.InsertRecoReleaseConfigs")
            insertRecoReleaseConfigsDAO.execute(binds = bindsInsert, transaction = False)

            updateRecoReleaseConfigsDAO = self.daoFactory(classname = "T0DataSvc.UpdateRecoReleaseConfigs")
            updateRecoReleaseConfigsDAO.execute(binds = bindsUpdate, transaction = False)

        return

    def lockDatasetsT0DataSvc(self):
        """
        _lockDatasetsT0DataSvc_

        Publish dataset information into the Tier0 Data Service.

        """
        getDatasetLockedDAO = self.daoFactory(classname = "T0DataSvc.GetDatasetLocked")
        datasetConfigs = getDatasetLockedDAO.execute(transaction = False)

        if len(datasetConfigs) > 0:

            bindsInsert = []
            bindsUpdate = []
            for config in datasetConfigs:
                bindsInsert.append( { 'PATH' : config['path'] } )
                bindsUpdate.append( { 'ID' : config['id'] } )

            insertDatasetLockedDAO = self.daoFactoryT0DataSvc(classname = "T0DataSvc.InsertDatasetLocked")
            insertDatasetLockedDAO.execute(binds = bindsInsert, transaction = False)

            updateDatasetLockedDAO = self.daoFactory(classname = "T0DataSvc.UpdateDatasetLocked")
            updateDatasetLockedDAO.execute(binds = bindsUpdate, transaction = False)

        return

    def terminate(self, params):
        """
        _terminate_

        Kill the code after one final pass when called by the master thread.

        """
        logging.debug("terminating immediately")
