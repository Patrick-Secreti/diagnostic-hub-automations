"""Original, independently reviewed ASE-style practice questions; never actual exam items."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo
import requests

ET = ZoneInfo("America/New_York")
CATEGORIES = [
    ("A6", "Electrical/Electronic Systems"),
    ("A8", "Engine Performance"),
    ("L1", "Advanced Engine Performance"),
    ("L3", "Light Duty Hybrid/Electric Vehicle Specialist"),
    ("L4", "Advanced Driver Assistance Systems Specialist"),
]
MODEL = "gpt-5.4-mini"

def api(prompt):
    response = requests.post("https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={"model": MODEL, "store": False, "reasoning":{"effort":"low"},
              "max_output_tokens": 3500, "input":prompt,
              "text":{"format":{"type":"json_object"}}},
        timeout=(8,90), allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise ValueError(f"OpenAI HTTP {response.status_code}")
    data = response.json()
    print("API usage:",json.dumps(data.get("usage",{})))
    if data.get("status") != "completed":
        raise ValueError("OpenAI response incomplete")
    return json.loads("".join(c.get("text","") for o in data.get("output",[])
                              for c in o.get("content",[]) if c.get("type")=="output_text"))

def normalize(text):
    return " ".join(re.findall(r"\w+",text.casefold()))

def fingerprint(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()

def validate(q):
    if not isinstance(q,dict):
        raise ValueError("Invalid question")
    for k,limit in (("stem",1000),("topic",120),("explanation",1200),("answer",1)):
        if not isinstance(q.get(k),str) or not 0<len(q[k].strip())<=limit:
            raise ValueError("Invalid "+k)
    if q["answer"] not in "ABCD" or len(q["answer"])!=1:
        raise ValueError("Invalid answer")
    options=q.get("options")
    if not isinstance(options,list) or len(options)!=4 or any(not isinstance(x,str) or not 0<len(x.strip())<=220 for x in options):
        raise ValueError("Invalid options")
    if len(set(map(normalize,options)))!=4:
        raise ValueError("Repeated options")
    return q

def generate(category,history):
    recent=[{"topic":r["question"]["topic"],"stem":r["question"]["stem"]} for r in history[-60:]]
    prompt=("Write ONE original ASE-style practice question for experienced independent automotive technicians. "
        "Not copied from an actual exam, study guide or question bank. Category: "+category[0]+" "+category[1]+". "
        "Use realistic electrical measurements, scan data, symptoms or a concrete technical scenario. "
        "Prefer diagnostic reasoning over trivia. Specify operating conditions, circuit topology or OEM assumptions whenever necessary. "
        "Four plausible distinct choices, exactly one defensible answer. Avoid ambiguous manufacturer-specific claims, unsafe live HV procedures, "
        "and security bypass instructions. Include a concise explanation of why the correct option is best and why alternatives are not. "
        "Avoid repeating the concepts or wording in this history: "+json.dumps(recent)+
        '. Return JSON only with keys stem, topic, options (four strings in A-D order), answer (A/B/C/D), explanation.')
    q=validate(api(prompt))
    if any(fingerprint(q["stem"])==fingerprint(r["question"]["stem"]) for r in history):
        raise ValueError("Duplicate question")
    # Separate blind review: reviewer sees options but not the proposed answer.
    review=api("Independently solve and critically review this original automotive practice question. "
        "Check for ambiguous assumptions, electrical/diagnostic errors, unsafe procedures, multiple defensible answers, "
        "and realistic measurements. If there is not exactly one technically defensible answer reject it. "
        "Return JSON only: {\"valid\":true or false,\"answer\":\"A/B/C/D\",\"reason\":\"short reasoning\"}. "
        +json.dumps({"category":category,"stem":q["stem"],"options":q["options"]}))
    if review.get("valid") is not True or review.get("answer")!=q["answer"]:
        raise ValueError("Independent answer review failed")
    return q

def save(path,state):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False))
    tmp.replace(path)

def deliver(hook,payload):
    # wait=true returns a message ID, rather than merely accepting a request.
    response=requests.post(hook,params={"wait":"true"},json=payload,timeout=20,allow_redirects=False)
    if not 200<=response.status_code<300:
        raise ValueError(f"Discord HTTP {response.status_code}")
    data=response.json()
    if not data.get("id"):
        raise ValueError("Discord message ID missing")
    return str(data["id"])

def question_payload(record):
    q=record["question"]
    return {"embeds":[{"title":f"ASE Practice Question #{record['number']:03d} | {record['category'][0]}",
        "color":0x2474A5,"description":q["stem"]+"\n\n"+"\n".join(f"**{letter}.** {option}" for letter,option in zip("ABCD",q["options"])),
        "footer":{"text":record["category"][1]+" | Original practice question · Not an official ASE exam item"}}],
        "poll":{"question":{"text":"Which answer is correct?"},"answers":[{"poll_media":{"text":letter}} for letter in "ABCD"],
                "duration":8,"allow_multiselect":False,"layout_type":1},
        "allowed_mentions":{"parse":[]}}

def answer_payload(record):
    q=record["question"]
    return {"embeds":[{"title":f"ASE Answer #{record['number']:03d} | {record['category'][0]}",
        "color":0x228B55,"description":f"**Correct answer: {q['answer']}. {q['options']['ABCD'.index(q['answer'])]}**\n\n{q['explanation']}",
        "footer":{"text":"Original ASE-style practice · Independent AI review, not official ASE material"}}],
        "allowed_mentions":{"parse":[]}}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state",type=Path,required=True)
    p.add_argument("--mode",choices=["question","answer"],required=True)
    p.add_argument("--publish",action="store_true")
    a=p.parse_args()
    today=datetime.now(ET).date().isoformat()
    state=json.loads(a.state.read_text()) if a.state.exists() else {"records":[]}
    history=state["records"]
    record=next((r for r in history if r["date"]==today),None)
    summary=[]
    try:
        if a.mode=="question":
            if record and record.get("question_id"):
                summary.append("Today's question already posted; duplicate suppressed.")
                return
            if record is None:
                category=CATEGORIES[len(history)%len(CATEGORIES)]
                q=generate(category,history)
                record={"date":today,"number":len(history)+1,"category":category,"question":q}
                if a.publish:
                    history.append(record)
                    save(a.state,state)
            payload=question_payload(record)
            summary.extend([f"Category: {record['category'][0]}",record["question"]["stem"],
                            *[f"{l}. {v}" for l,v in zip("ABCD",record["question"]["options"])]])
        else:
            if not record or not record.get("question_id"):
                summary.append("No confirmed question today; answer not posted.")
                return
            if record.get("answer_id"):
                summary.append("Today's answer already posted; duplicate suppressed.")
                return
            payload=answer_payload(record)
            summary.append(f"Answer to question #{record['number']:03d}")
        if not a.publish:
            summary.append("Preview only — no Discord delivery.")
            return
        key=a.mode+"_id"
        pending=a.mode+"_pending"
        # A previous ambiguous send must be investigated, not blindly repeated.
        if record.get(pending):
            raise ValueError("Previous delivery ambiguous; check Discord before retrying")
        hook=os.environ["ASE_DISCORD_WEBHOOK_URL"]
        record[pending]=True
        save(a.state,state)
        message_id=deliver(hook,payload)
        record[key]=message_id
        record.pop(pending,None)
        save(a.state,state)
        summary.append("Confirmed Discord delivery: "+message_id)
    finally:
        text="\n\n".join(summary)
        print(text)
        path=os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path,"a",encoding="utf-8") as out:
                print("## ASE Practice — "+a.mode+"\n\n"+text,file=out)

if __name__=="__main__":
    main()
