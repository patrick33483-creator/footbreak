// Runs via stdin inside existing container. No secrets leave its environment.
import fs from 'node:fs';
import {matchAll} from '/app/rule_matcher.js';
const p = JSON.parse(fs.readFileSync(0,'utf8'));
if (p.op === 'match') {
  const rule = JSON.parse(fs.readFileSync('/app/data/rules.json','utf8')).rules.find(r=>r.id==='ch-N2');
  console.log(JSON.stringify({rule, hits:p.matches.flatMap(m=>matchAll(m,[rule]).map(h=>({
    sid:m.sid,league:m.league,home:m.home,away:m.away,kickoff_utc:m.kickoff_utc,
    t5_at:m.snapshots.T5_OU.captured_at,line:h.match.t5Line,odds:h.match.t5Dec,
  })))}));
} else {
  // Fixed independent monitoring destination. NEVER inherit TELEGRAM_CHAT_ID.
  if (String(p.chat_id) !== '703318555') throw new Error('Monitoring destination mismatch');
  const token=process.env.TELEGRAM_BOT_TOKEN;
  if(!token) throw new Error('Monitoring bot unavailable');
  const method=p.op==='verify-chat'?'getChat':p.op==='notify'?'sendMessage':null;
  if(!method)throw new Error('Unknown operation');
  const body={chat_id:'703318555'};
  if(method==='sendMessage')Object.assign(body,{text:p.text,disable_web_page_preview:true});
  try {
    const res=await fetch(`https://api.telegram.org/bot${token}/${method}`,{
      method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),
      signal:AbortSignal.timeout(15000),
    });
    const j=await res.json();
    if(!res.ok||!j.ok){console.log(JSON.stringify({ok:false,http_status:res.status,description:j.description}));}
    else {
      const chat=method==='getChat'?j.result:j.result.chat;
      if(chat.type!=='private'||String(chat.id)!=='703318555')throw new Error('Not approved private chat');
      console.log(JSON.stringify({ok:true,chat_id:chat.id,chat_type:chat.type,
        username:chat.username,message_id:j.result.message_id,date:j.result.date}));
    }
  } catch(e) {
    // Timeouts may occur after acceptance. Do not retry ambiguous sends.
    console.log(JSON.stringify({ok:false,uncertain:method==='sendMessage',error_type:e.name}));
  }
}
