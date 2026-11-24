"""Weekly fictional diagnostic cases with blind technical review."""
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from ase_question import ET, api, deliver, save, fingerprint

FIELDS = {"vehicle":180,"concern":450,"codes":250,"findings":950,
          "additional":1000,"root_cause":350,"diagnosis":1000,"resolution":600,"takeaway":400}
STAGES = ("case","hint","resolution")

def validate(c):
    if not isinstance(c,dict):
        raise ValueError("Invalid case")
    for k,n in FIELDS.items():
        if not isinstance(c.get(k),str) or not 0<len(c[k].strip())<=n:
            raise ValueError("Invalid "+k)
    return c

def generate(history):
    recent=[{k:r["case"][k] for k in ("vehicle","concern","root_cause")} for r in history[-60:]]
    feedback=""
    for attempt in range(3):
        try:
            c=validate(api("Create one original FICTIONAL automotive diagnostic case for experienced technicians. "
                "Use realistic vehicle architecture, fault codes and measured data; distinguish measured values from assumptions. "
                "Avoid invented OEM specifications, unsafe procedures and security bypass. Initial findings should permit multiple hypotheses, not reveal the answer. "
                "Wednesday additional findings must narrow the fault with a specific discriminating test; Friday diagnosis must actually isolate the root cause, with repair and verification. "
                "Keep all stages consistent. Return JSON string fields only, with maximum lengths: "+json.dumps(FIELDS)+
                ". Avoid repeating these prior cases: "+json.dumps(recent)+". Review feedback: "+feedback))
            if any(fingerprint(c["concern"]+c["findings"])==fingerprint(r["case"]["concern"]+r["case"]["findings"]) for r in history):
                raise ValueError("Duplicate case")
            review=api("Critically review this fictional diagnostic case for realistic vehicle architecture, codes, measurements, safe tests, consistency across stages, whether evidence actually isolates root cause, and whether repair verification is sufficient. "
                "Reject unsupported conclusions or contradictory facts. Return JSON with valid boolean and reason string. "+json.dumps(c))
            if review.get("valid") is not True:
                raise ValueError("Review rejected: "+str(review.get("reason",""))[:400])
            print("Independent technical review passed")
            return c
        except ValueError as e:
            feedback=str(e)
            if feedback.startswith(("OpenAI HTTP","OpenAI response incomplete")):
                raise
            print("Case rejected:",feedback)
    raise ValueError("No case passed review after three attempts")

def payload(record,stage):
    c=record["case"]
    number=record["number"]
    if stage=="case":
        title=f"Diagnostic Challenge #{number:03d}"
        color=0x2474A5
        body=f"**Vehicle:** {c['vehicle']}\n\n**Customer concern:** {c['concern']}\n\n**Fault codes:** {c['codes']}\n\n**Initial findings**\n{c['findings']}\n\n**What would you test next, and why?**\nDiscuss your diagnostic approach below."
    elif stage=="hint":
        title=f"Challenge #{number:03d} — Additional Findings"
        color=0xD99A32
        body=f"**Vehicle:** {c['vehicle']}\n\n{c['additional']}\n\n**Does this change your diagnostic direction?**"
    else:
        title=f"Challenge #{number:03d} — Case Resolution"
        color=0x228B55
        body=f"**Vehicle:** {c['vehicle']}\n\n**Root cause:** {c['root_cause']}\n\n**Diagnostic method**\n{c['diagnosis']}\n\n**Resolution**\n{c['resolution']}\n\n**Key takeaway:** {c['takeaway']}"
    return {"embeds":[{"title":title,"color":color,"description":body,
             "footer":{"text":"The Diagnostic Hub · Fictional training scenario · AI-reviewed"}}],
             "allowed_mentions":{"parse":[]}}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state",type=Path,required=True)
    p.add_argument("--mode",choices=STAGES,required=True)
    p.add_argument("--publish",action="store_true")
    a=p.parse_args()
    today=datetime.now(ET).date()
    week=(today-timedelta(days=today.weekday())).isoformat()
    state=json.loads(a.state.read_text()) if a.state.exists() else {"records":[]}
    history=state["records"]
    record=next((r for r in history if r["week"]==week),None)
    notes=[]
    status="Not delivered"
    try:
        if a.mode=="case" and record is None:
            record={"week":week,"number":len(history)+1,"case":generate(history)}
            if a.publish:
                history.append(record)
                save(a.state,state)
        if record is None:
            status="Skipped — no case for this week"
            return
        idx=STAGES.index(a.mode)
        if idx and not all(record.get(s+"_id") for s in STAGES[:idx]):
            status="Skipped — earlier stage not confirmed"
            return
        body=payload(record,a.mode)
        notes.append(body["embeds"][0]["description"])
        if record.get(a.mode+"_id"):
            status="Already delivered — duplicate suppressed"
            return
        if not a.publish:
            status="Preview only — no Discord delivery"
            return
        pending=a.mode+"_pending"
        if record.get(pending):
            raise ValueError("Previous delivery ambiguous; check Discord before retrying")
        hook=os.environ["CHALLENGE_DISCORD_WEBHOOK_URL"]
        record[pending]=True
        save(a.state,state)
        message_id=deliver(hook,body)
        record[a.mode+"_id"]=message_id
        record.pop(pending,None)
        save(a.state,state)
        status="Confirmed Discord delivery: "+message_id
    finally:
        summary=f"## Weekly Diagnostic Challenge\n\n| Metric | Result |\n|---|---|\n| Week | {week} |\n| Stage | {a.mode} |\n| Status | {status} |\n| Case number | {record['number'] if record else '—'} |\n| Published cases | {sum(bool(r.get('case_id')) for r in history)} |\n\n"+"\n\n".join(notes)
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"],"a",encoding="utf-8") as f:
                f.write(summary+"\n")

if __name__=="__main__":
    main()
