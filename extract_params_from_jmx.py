# extract_params_from_jmx.py
import xml.etree.ElementTree as ET


def extract_editable_params(jmx_path):
    tree = ET.parse(jmx_path)
    root = tree.getroot()

    params = {
        "thread_groups": [],
        "http_samplers": []
    }

    def get_prop(elem, name, types=("boolProp", "stringProp", "intProp", "longProp")):
        for tag in types:
            for child in elem.findall(f"./{tag}"):
                if child.attrib.get("name") == name:
                    # Handle booleans properly
                    if tag == "boolProp":
                        return child.text.strip().lower() == "true"
                    return child.text
        return None

    ## ----------- Extract Thread Groups -----------
    for tg in root.iter("ThreadGroup"):
        tg_name = tg.attrib.get("testname", "Thread Group")

        # Find main_controller for loop settings
        main_controller = None
        for elem in tg.findall("elementProp"):
            if elem.attrib.get("name") == "ThreadGroup.main_controller":
                main_controller = elem
                break

        loop_count = None
        infinite_loop = False
        if main_controller is not None:
            loop_count = get_prop(main_controller, "LoopController.loops")
            infinite_loop = get_prop(main_controller, "LoopController.continue_forever") or False

        params["thread_groups"].append({
            "name": tg_name,
            "num_threads": get_prop(tg, "ThreadGroup.num_threads"),
            "ramp_time": get_prop(tg, "ThreadGroup.ramp_time"),
            "loop_count": loop_count,
            
            "duration": get_prop(tg, "ThreadGroup.duration"),
            "startup_delay": get_prop(tg, "ThreadGroup.delay"),
            "specify_thread_lifetime": get_prop(tg, "ThreadGroup.scheduler") or False,
            "same_user_on_iteration": get_prop(tg, "ThreadGroup.same_user_on_next_iteration") or False,
            "delay_thread_creation": get_prop(tg, "ThreadGroup.delayedStart") or False,
            "sampler_error_action": get_prop(tg, "ThreadGroup.on_sample_error") or "continue" 
            # values typically: continue, startnextloop, stopthread, stoptest, stoptestnow
        })

    ## ----------- Extract HTTP Samplers -----------
    for sampler in root.iter("HTTPSamplerProxy"):
        sampler_name = sampler.attrib.get("testname", "Sampler")
        params["http_samplers"].append({
            "name": sampler_name,
            "domain": get_prop(sampler, "HTTPSampler.domain"),
            "path": get_prop(sampler, "HTTPSampler.path"),
            "method": get_prop(sampler, "HTTPSampler.method"),
            "port": get_prop(sampler, "HTTPSampler.port"),
            "protocol": get_prop(sampler, "HTTPSampler.protocol"),
            "timeout": get_prop(sampler, "HTTPSampler.connect_timeout"),
        })

    return params
