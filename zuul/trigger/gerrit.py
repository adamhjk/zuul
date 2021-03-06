# Copyright 2012 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import threading
import logging
from zuul.lib import gerrit
from zuul.model import TriggerEvent, Change


class GerritEventConnector(threading.Thread):
    """Move events from Gerrit to the scheduler."""

    log = logging.getLogger("zuul.GerritEventConnector")

    def __init__(self, gerrit, sched):
        super(GerritEventConnector, self).__init__()
        self.gerrit = gerrit
        self.sched = sched
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.gerrit.addEvent(None)

    def _handleEvent(self):
        data = self.gerrit.getEvent()
        if self._stopped:
            return
        event = TriggerEvent()
        event.type = data.get('type')
        change = data.get('change')
        if change:
            event.project_name = change.get('project')
            event.branch = change.get('branch')
            event.change_number = change.get('number')
            event.change_url = change.get('url')
            patchset = data.get('patchSet')
            if patchset:
                event.patch_number = patchset.get('number')
                event.refspec = patchset.get('ref')
            event.approvals = data.get('approvals', [])
            event.comment = data.get('comment')
        refupdate = data.get('refUpdate')
        if refupdate:
            event.project_name = refupdate.get('project')
            event.ref = refupdate.get('refName')
            event.oldrev = refupdate.get('oldRev')
            event.newrev = refupdate.get('newRev')
        self.sched.addEvent(event)
        self.gerrit.eventDone()

    def run(self):
        while True:
            if self._stopped:
                return
            try:
                self._handleEvent()
            except:
                self.log.exception("Exception moving Gerrit event:")


class Gerrit(object):
    log = logging.getLogger("zuul.Gerrit")

    def __init__(self, config, sched):
        self.sched = sched
        server = config.get('gerrit', 'server')
        user = config.get('gerrit', 'user')
        if config.has_option('gerrit', 'sshkey'):
            sshkey = config.get('gerrit', 'sshkey')
        else:
            sshkey = None
        if config.has_option('gerrit', 'port'):
            port = config.get('gerrit', 'port')
        else:
            port = 29418
        self.gerrit = gerrit.Gerrit(server, user, port, sshkey)
        self.gerrit.startWatching()
        self.gerrit_connector = GerritEventConnector(
            self.gerrit, sched)
        self.gerrit_connector.start()

    def stop(self):
        self.gerrit_connector.stop()
        self.gerrit_connector.join()

    def report(self, change, message, action):
        self.log.debug("Report change %s, action %s, message: %s" %
                       (change, action, message))
        if not change.number:
            self.log.debug("Change has no number; not reporting")
            return
        if not action:
            self.log.debug("No action specified; not reporting")
            return
        changeid = '%s,%s' % (change.number, change.patchset)
        return self.gerrit.review(change.project.name, changeid,
                                  message, action)

    def isMerged(self, change):
        self.log.debug("Checking if change %s is merged" % change)
        if not change.number:
            self.log.debug("Change has no number; considering it merged")
            # Good question.  It's probably ref-updated, which, ah,
            # means it's merged.
            return True

        data = self.gerrit.query(change.number)
        change._data = data
        change.is_merged = self._isMerged(change)
        return change.is_merged

    def _isMerged(self, change):
        data = change._data
        if not data:
            return False
        status = data.get('status')
        if not status:
            return False
        self.log.debug("Change %s status: %s" % (change, status))
        if status == 'MERGED' or status == 'SUBMITTED':
            return True

    def _canMerge(self, change, allow_needs):
        if not change.number:
            self.log.debug("Change has no number; considering it merged")
            # Good question.  It's probably ref-updated, which, ah,
            # means it's merged.
            return True
        data = change._data
        if not data:
            return False
        if not 'submitRecords' in data:
            return False
        try:
            for sr in data['submitRecords']:
                if sr['status'] == 'OK':
                    return True
                elif sr['status'] == 'NOT_READY':
                    for label in sr['labels']:
                        if label['status'] == 'OK':
                            continue
                        elif label['status'] in ['NEED', 'REJECT']:
                            # It may be our own rejection, so we ignore
                            if label['label'].lower() not in allow_needs:
                                return False
                            continue
                        else:
                            # IMPOSSIBLE
                            return False
                else:
                    # CLOSED, RULE_ERROR
                    return False
        except:
            self.log.exception("Exception determining whether change"
                               "%s can merge:" % change)
            return False
        return True

    def getChange(self, number, patchset, queue_name, changes=None):
        # TODO: queue_name is screwing up the data model, refactor
        # the queue context so it isn't necessary.
        self.log.info("Getting information for %s,%s" % (number, patchset))
        if changes is None:
            changes = {}
        data = self.gerrit.query(number)
        project = self.sched.projects[data['project']]
        change = Change(queue_name, project)
        change._data = data

        change.number = number
        change.patchset = patchset
        change.project = project
        change.branch = data['branch']
        change.url = data['url']
        max_ps = 0
        for ps in data['patchSets']:
            if ps['number'] == patchset:
                change.refspec = ps['ref']
            if int(ps['number']) > int(max_ps):
                max_ps = ps['number']
        if max_ps == patchset:
            change.is_current_patchset = True
        else:
            change.is_current_patchset = False

        manager = self.sched.queue_managers[queue_name]
        change.can_merge = self._canMerge(change,
                                          manager.getSubmitAllowNeeds())
        change.is_merged = self._isMerged(change)
        if change.is_merged:
            # This change is merged, so we don't need to look any further
            # for dependencies.
            return change

        key = '%s,%s' % (number, patchset)
        changes[key] = change

        def cachedGetChange(num, ps):
            key = '%s,%s' % (num, ps)
            if key in changes:
                return changes.get(key)
            c = self.getChange(num, ps, queue_name, changes)
            return c

        if 'dependsOn' in data:
            parts = data['dependsOn'][0]['ref'].split('/')
            dep_num, dep_ps = parts[3], parts[4]
            dep = cachedGetChange(dep_num, dep_ps)
            if not dep.is_merged:
                change.needs_change = dep

        if 'neededBy' in data:
            for needed in data['neededBy']:
                parts = needed['ref'].split('/')
                dep_num, dep_ps = parts[3], parts[4]
                dep = cachedGetChange(dep_num, dep_ps)
                if not dep.is_merged and dep.is_current_patchset:
                    change.needed_by_changes.append(dep)

        return change
