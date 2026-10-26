import os
import importlib
import traceback

CHECKERS_DIR = os.path.dirname(__file__)

def _load_all():
    gateways = {}
    print("[checkers] Scanning: " + CHECKERS_DIR)
    for filename in sorted(os.listdir(CHECKERS_DIR)):
        if not filename.endswith(".py"):
            continue
        if filename.startswith("_"):
            continue
        module_name = filename[:-3]
        try:
            module = importlib.import_module("checkers." + module_name)
            if not hasattr(module, "NAME"):
                print("[checkers] Skipped " + filename + ": no NAME")
                continue
            if not hasattr(module, "check"):
                print("[checkers] Skipped " + filename + ": no check()")
                continue
            name = getattr(module, "NAME", module_name)
            cost = getattr(module, "COST", "Free / check")
            gateways[module_name] = {
                "id": module_name,
                "name": name,
                "cost": cost,
                "func": module.check,
            }
            print("[checkers] Loaded: " + name + " (" + module_name + ")")
        except Exception as e:
            print("[checkers] Failed to load " + filename + ": " + str(e))
            traceback.print_exc()
    print("[checkers] Total gateways loaded: " + str(len(gateways)))
    return gateways

GATEWAYS = _load_all()

def get_gateway_list():
    result = []
    for gw in GATEWAYS.values():
        result.append({"id": gw["id"], "name": gw["name"], "cost": gw["cost"]})
    return result

def run_check(gateway_id, url):
    if gateway_id not in GATEWAYS:
        return {
            "url": url,
            "status": "error",
            "redirects_to": None,
            "response_time_ms": None,
            "error": "Unknown gateway: " + gateway_id
        }
    try:
        return GATEWAYS[gateway_id]["func"](url)
    except Exception as e:
        return {
            "url": url,
            "status": "error",
            "redirects_to": None,
            "response_time_ms": None,
            "error": "Checker crashed: " + str(e)[:150]
        }