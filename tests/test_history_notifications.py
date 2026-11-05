"""Exercise the actual web refresh function with local status responses only."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class HistoryNotificationTests(unittest.TestCase):
    def test_checking_reconfirmation_new_completion_and_snooze_reminder(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const begin = source.indexOf('  function captureConversationFocus() {');
const end = source.indexOf('  async function refresh(force', begin);
if (begin < 0 || end < 0) throw Error('Refresh function not found');
function block(from, until) {
  const start = source.indexOf(from), stop = source.indexOf(until, start);
  if (start < 0 || stop < 0) throw Error('Refresh dependency not found');
  return source.slice(start, stop);
}
const labels = source.match(/^  const labels = .*;$/m)?.[0];
if (!labels) throw Error('Status labels not found');
let response;
let sounds = 0;
const announcements = [];
const updates = {_text:''};
Object.defineProperty(updates, 'textContent', {
  get(){return this._text;}, set(value){this._text=value;announcements.push(value);}
});
const context = {state:null, previousStatuses:new Map(), previousAnnouncedCounts:null,
  pending:new Map(), activeTab:'board', elements:{'rename-dialog':{open:false},'conversation-updates':updates},
  request:async()=>response, isSnoozed:session=>Boolean(session.snoozed),
  playAttentionSound:()=>sounds++, render:()=>{},
  document:{activeElement:null,visibilityState:'visible',hasFocus:()=>true}, Date, Map};
vm.createContext(context);
vm.runInContext(labels + '\n'
  + block('  function setText(', '  function make(')
  + block('  function visibleStatus(', '  function makeCard(')
  + source.slice(begin,end),context);
const base = {key:'claude:fixture',status:'ready',event_id:'complete-1',notification_eligible:true};
async function step(session) {
  response={sessions:[session],history:[],csrf_token:'fixture',now:1};
  await context.refreshOnce();
  if (!context.connected) throw Error('Refresh failed');
}
(async()=>{
 await step({...base, status:'disconnected',history_checking:true,confirmed_status:'ready',notification_eligible:false});
 if(announcements.length!==0) throw Error('Initial snapshot announced conversation counts');
 await step({...base, reconfirmation_silent:true});
 if(announcements.length!==1 || !announcements[0].includes('1 conversation is ready to review.'))
   throw Error('Actual count announcer did not receive the refreshed visible status');
 if(sounds!==0) throw Error('Cold reconfirmation replayed sound');
 await step({...base,event_id:'complete-2'});
 if(sounds!==1) throw Error('New completion did not sound');
 if(announcements.length!==1) throw Error('Same-count new event replayed aggregate announcement');
 await step({...base,event_id:'complete-2',status:'disconnected',history_checking:true,confirmed_status:'ready',notification_eligible:false});
 await step({...base,event_id:'complete-2',reconfirmation_silent:true});
 if(sounds!==1) throw Error('Warm reconfirmation replayed sound');
 await step({...base,event_id:'complete-2',snoozed:true});
 await step({...base,event_id:'complete-2'});
 if(sounds!==2) throw Error('Snooze reminder did not sound');
 if(announcements.length!==3) throw Error('Metadata/snooze changes replayed aggregate counts');
 process.stdout.write(JSON.stringify({sounds,announcements:announcements.length,passed:true}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([node, "-e", script, str(Path(__file__).resolve().parents[1] / "static/app.js")],
                text=True, capture_output=True, timeout=10,
                env={"HOME": home, "PATH": os.defpath, "LANG": "en_US.UTF-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"sounds": 2, "announcements": 3, "passed": True})
