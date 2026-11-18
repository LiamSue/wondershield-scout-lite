"""Versioned end-to-end result contract; evaluator not yet implemented.
No raw file read, inference, authorization or execution occurs here.
"""
SCHEMA = "wondershield.task/1"
def evaluate_task(request):
    valid = (type(request) is dict and set(request)=={"schema","task_id","task_type","sources"}
             and request.get("schema")==SCHEMA and type(request.get("task_id")) is str
             and 0 < len(request["task_id"]) <= 128 and type(request.get("task_type")) is str
             and request["task_type"]=="supplier_delivery_delay" and type(request.get("sources")) is list
             and 0 < len(request["sources"]) <= 32 and all(type(x) is dict and set(x)=={"system","object_id"}
             and all(type(v) is str and 0<len(v)<=128 for v in x.values()) for x in request["sources"]))
    return {"schema":SCHEMA,"status":"NOT_IMPLEMENTED" if valid else "INVALID_REQUEST",
            "reason":"PUBLIC_TASK_BINDING_NOT_IMPLEMENTED" if valid else "TASK_CONTRACT_INVALID",
            "evidence":{"sufficiency":"NOT_EVALUATED","conflict":"NOT_EVALUATED","freshness":"NOT_EVALUATED","references":[]},
            "data":{"feasibility":"NOT_EVALUATED","missing_conditions":[]},
            "tool":{"declared":False,"reachable":"NOT_EVALUATED","verified":False},
            "authorization":{"decision":"NOT_EVALUATED","decision_ref":None},
            "execution":{"status":"NOT_PERFORMED","result":None,"receipt":None},
            "coverage":{"expected_targets":len(request["sources"]) if valid else None,"selected":0,"checked":0,"unprocessed_reason":"TASK_BINDING_NOT_IMPLEMENTED"}}
