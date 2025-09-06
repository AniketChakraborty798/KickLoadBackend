import boto3
import time
import os

def scale_asg_for_vus(num_threads):
    # Estimate how many workers needed, e.g. 1 m5.2xlarge per 50,000 VU
    WORKER_CAPACITY = 50000
    needed = min(max(1, (num_threads + WORKER_CAPACITY - 1) // WORKER_CAPACITY), 20)
    client = boto3.client('autoscaling', region_name=os.getenv('AWS_DEFAULT_REGION'))
    client.update_auto_scaling_group(
        AutoScalingGroupName=os.getenv('JMETER_ASG_NAME'),
        DesiredCapacity=needed
    )
    logger.info(f"Requested ASG {os.getenv('JMETER_ASG_NAME')} scale to {needed} for {num_threads} threads")

def discover_asg_worker_ips(timeout=600):
    # Wait up to timeout seconds for workers to become healthy, then return their private IPs
    ec2 = boto3.resource('ec2', region_name=os.getenv('AWS_DEFAULT_REGION'))
    asg_name = os.getenv('JMETER_ASG_NAME')
    tag_key = os.getenv('JMETER_WORKER_TAG_KEY')
    tag_value = os.getenv('JMETER_WORKER_TAG_VALUE')
    client = boto3.client('autoscaling', region_name=os.getenv('AWS_DEFAULT_REGION'))

    waited = 0
    while waited < timeout:
        inst_ids = []
        asg = client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = asg.get('AutoScalingGroups', [])
        if groups:
            for inst in groups[0]['Instances']:
                if inst['LifecycleState'] == 'InService' and inst['HealthStatus'] == 'Healthy':
                    inst_ids.append(inst['InstanceId'])
        if len(inst_ids) == groups[0]['DesiredCapacity']:
            # All desired workers ready
            break
        time.sleep(10)
        waited += 10

    ip_addrs = []
    for i in ec2.instances.filter(InstanceIds=inst_ids):
        for tag in i.tags or []:
            if tag['Key'] == tag_key and tag['Value'] == tag_value:
                ip_addrs.append(i.private_ip_address)
    logger.info(f"Discovered ASG worker IPs: {ip_addrs}")
    return ip_addrs

def scale_asg_down_to_zero():
    client = boto3.client('autoscaling', region_name=os.getenv('AWS_DEFAULT_REGION'))
    client.update_auto_scaling_group(
        AutoScalingGroupName=os.getenv('JMETER_ASG_NAME'),
        DesiredCapacity=0
    )
    logger.info(f"Scaled ASG {os.getenv('JMETER_ASG_NAME')} down to 0")
