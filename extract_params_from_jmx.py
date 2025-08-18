# extract_params_from_jmx.py
import xml.etree.ElementTree as ET

def extract_editable_params(jmx_path):
    tree = ET.parse(jmx_path)
    root = tree.getroot()

    params = {
        "thread_groups": [],
        "http_samplers": []
    }

    def get_prop(elem, name):
        """Find a value from intProp, stringProp, or longProp."""
        for tag in ["stringProp", "intProp", "longProp"]:
            val = elem.findtext(f".//{tag}[@name='{name}']")
            if val is not None:
                return val
        return None

    # ✅ Thread Groups
    for tg in root.iter("ThreadGroup"):
        tg_name = tg.attrib.get("testname", "Thread Group")
        params["thread_groups"].append({
            "name": tg_name,
            "num_threads": get_prop(tg, "ThreadGroup.num_threads"),
            "ramp_time": get_prop(tg, "ThreadGroup.ramp_time"),
            "loop_count": get_prop(tg, "LoopController.loops"),
            "duration": get_prop(tg, "ThreadGroup.duration"),
            "delay": get_prop(tg, "ThreadGroup.delay")
        })

    # ✅ HTTP Samplers
    for sampler in root.iter("HTTPSamplerProxy"):
        sampler_name = sampler.attrib.get("testname", "Sampler")
        params["http_samplers"].append({
            "name": sampler_name,
            "domain": get_prop(sampler, "HTTPSampler.domain"),
            "path": get_prop(sampler, "HTTPSampler.path"),
            "method": get_prop(sampler, "HTTPSampler.method")
        })

    return params
