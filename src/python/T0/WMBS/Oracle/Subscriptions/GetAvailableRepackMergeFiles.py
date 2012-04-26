"""
_GetAvailableRepackMergeFiles_

Oracle implementation of GetAvailableRepackMergeFiles

Similar to Subscriptions.GetAvailableFiles,
except also returns lumi information
"""

from WMCore.Database.DBFormatter import DBFormatter

class GetAvailableRepackMergeFiles(DBFormatter):

    def execute(self, subscription, conn = None, transaction = False):

        #
        # repack merge input files can be either multiples
        # files per lumi (in which case the active split lumis
        # protections are needed) or a single file can also
        # contain one or more full lumis
        #
        # For the later case the splitting algorithm cares
        # about knwowing the first and last lumi for a file
        #
        # repackmerge subscriptions are run specific
        #

        sql = """SELECT wmbs_sub_files_available.fileid AS id,
                        wmbs_file_details.filesize AS filesize,
                        wmbs_file_details.events AS events,
                        wmbs_file_details.lfn AS lfn,
                        wmbs_location.se_name AS location,
                        MIN(wmbs_file_runlumi_map.lumi) AS first_lumi,
                        MAX(wmbs_file_runlumi_map.lumi) AS last_lumi
                 FROM wmbs_sub_files_available
                 INNER JOIN wmbs_file_runlumi_map ON
                   wmbs_file_runlumi_map.fileid = wmbs_sub_files_available.fileid
                 INNER JOIN wmbs_file_details ON
                   wmbs_file_details.id = wmbs_sub_files_available.fileid
                 INNER JOIN wmbs_file_location ON
                   wmbs_file_location.fileid = wmbs_sub_files_available.fileid
                 INNER JOIN wmbs_location ON
                   wmbs_location.id = wmbs_file_location.location
                 INNER JOIN wmbs_subscription ON
                   wmbs_subscription.id = wmbs_sub_files_available.subscription
                 INNER JOIN wmbs_workflow_output ON
                   wmbs_workflow_output.output_fileset = wmbs_subscription.fileset
                 INNER JOIN run_stream_fileset_assoc ON
                   run_stream_fileset_assoc.run_id = wmbs_file_runlumi_map.run AND
                   run_stream_fileset_assoc.fileset =
                     ( SELECT fileset FROM wmbs_subscription WHERE workflow = wmbs_workflow_output.workflow_id )
                 LEFT OUTER JOIN lumi_section_split_active ON
                   lumi_section_split_active.run_id = wmbs_file_runlumi_map.run AND
                   lumi_section_split_active.lumi_id = wmbs_file_runlumi_map.lumi AND
                   lumi_section_split_active.stream_id = run_stream_fileset_assoc.stream_id
                 WHERE wmbs_sub_files_available.subscription = :subscription
                 AND lumi_section_split_active.run_id IS NULL
                 GROUP BY wmbs_sub_files_available.fileid,
                          wmbs_file_details.filesize,
                          wmbs_file_details.events,
                          wmbs_file_details.lfn,
                          wmbs_location.se_name
                 """

        results = self.dbi.processData(sql, { 'subscription' : subscription },
                                       conn = conn, transaction = transaction)

        return self.formatDict(results)