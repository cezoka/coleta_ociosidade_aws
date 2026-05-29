import boto3, csv, os
from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError

# --- CONFIGURAÇÕES ---
# Nome da role (função) configurada nas contas filhas que permite acesso cross-account
# Pela sua empresa gerenciar via AWS Control Tower, a role chama AWSControlTowerExecution.
ROLE_NAME = 'AWSControlTowerExecution'

# Nome do bucket S3 onde o relatório será salvo (deve ser único globalmente)
BUCKET_NAME = 'auditoria-ociosidade' # <-- ALTERE PARA UM NOME ÚNICO
REGION = 'us-east-1' # Região principal

end_time = datetime.now(timezone.utc)
start_time_90 = end_time - timedelta(days=90)
start_time_30 = end_time - timedelta(days=30)
threshold_date_60 = end_time - timedelta(days=60) # Limite de 2 meses para EC2/EBS novos

# Snapshots - O usuário solicitou manualmente 'Anteriores à Feveireiro/2026'
limit_snapshot_date = datetime(2026, 2, 1, tzinfo=timezone.utc)

# Tabela genérica para ter base financeira aproximada pra EC2/RDS 
EC2_PRICES_MONTHLY = {
    't2.micro': 8.5, 't2.small': 17.0, 't2.medium': 34.0, 't2.large': 68.0,
    't3.micro': 7.5, 't3.small': 15.0, 't3.medium': 30.0, 't3.large': 60.0, 't3.xlarge': 120.0,
    't3a.micro': 6.8, 't3a.small': 13.5, 't3a.medium': 27.0, 't3a.large': 54.0, 't3a.xlarge': 108.0,
    'm5.large': 70.0, 'm5.xlarge': 140.0, 'm5.2xlarge': 280.0,
    'c5.large': 62.0, 'c5.xlarge': 124.0, 'c5.2xlarge': 248.0,
    'r5.large': 92.0, 'r5.xlarge': 184.0, 'r5.2xlarge': 368.0
}

CACHE_PRICES_MONTHLY = {
    'cache.t2.micro': 12.0, 'cache.t2.small': 24.0, 'cache.t2.medium': 48.0,
    'cache.t3.micro': 10.0, 'cache.t3.small': 20.0, 'cache.t3.medium': 40.0,
    'cache.t4g.micro': 8.5, 'cache.t4g.small': 17.0, 'cache.t4g.medium': 34.0,
    'cache.m5.large': 110.0, 'cache.m5.xlarge': 220.0,
    'cache.r5.large': 150.0, 'cache.r5.xlarge': 300.0
}

def get_ec2_cost(instance_type):
    # Base de instâncias genéricas, se o modelo for absurdo cobra default=50USD
    return EC2_PRICES_MONTHLY.get(instance_type, 50.0) 

def create_s3_bucket(s3_client, bucket_name, region):
    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ['404', '403']:
            try:
                if region == 'us-east-1':
                    s3_client.create_bucket(Bucket=bucket_name)
                else:
                    s3_client.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={'LocationConstraint': region})
            except Exception as create_err:
                raise create_err
        else:
            raise

def fetch_cw_metrics(cw_client, queries, st, et):
    results = {}
    for i in range(0, len(queries), 500):
        print(f"-> Baixando lote de métricas CloudWatch...")
        res = cw_client.get_metric_data(MetricDataQueries=queries[i:i+500], StartTime=st, EndTime=et)
        for r in res.get('MetricDataResults', []):
            if r['Values']:
                results[r['Id']] = sum(r['Values']) / len(r['Values'])
            else:
                results[r['Id']] = 0.0
    return results

def assume_role(account_id, role_name):
    sts = boto3.client('sts')
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    try:
        response = sts.assume_role(RoleArn=role_arn, RoleSessionName="AuditoriaOciosidade")
        return response['Credentials']
    except:
        return None

def extract_idle_resources(ec2_client, cw_client, rds_client, elbv2_client, logs_client, ddb_client, cache_client, account_name):
    idle = []
    
    # 1. EC2 Zumbis (últimos 90 dias)
    print(f"\n--- MAPEANDO INSTÂNCIAS EC2 ---")
    instances = [i for r in ec2_client.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]).get('Reservations', []) for i in r.get('Instances', [])]
    queries_90, inst_data = [], {}
    
    for inst in instances:
        iid = inst['InstanceId']
        itype = inst.get('InstanceType', 'unknown')
        name = next((t['Value'] for t in inst.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
        
        if inst['LaunchTime'] > threshold_date_60: continue
            
        qid = iid.replace('-', '_') 
        inst_data[qid] = {'id': iid, 'name': name, 'type': itype}
        for m_name, stat, label in [('CPUUtilization', 'Average', 'cpu'), ('mem_used_percent', 'Average', 'mem'), ('NetworkIn', 'Sum', 'netin'), ('NetworkOut', 'Sum', 'netout')]:
            ns = 'CWAgent' if label == 'mem' else 'AWS/EC2'
            queries_90.append({'Id': f"{label}_{qid}", 'MetricStat': {'Metric': {'Namespace': ns, 'MetricName': m_name, 'Dimensions': [{'Name': 'InstanceId', 'Value': iid}]}, 'Period': 86400, 'Stat': stat}, 'ReturnData': True})

    if queries_90:
        metrics_90 = fetch_cw_metrics(cw_client, queries_90, start_time_90, end_time)
        for qid, data in inst_data.items():
            cpu = metrics_90.get(f"cpu_{qid}")
            mem = metrics_90.get(f"mem_{qid}")
            net = (metrics_90.get(f"netin_{qid}", 0) + metrics_90.get(f"netout_{qid}", 0))
            if cpu is not None and cpu < 10.0 and net < (5 * 1024 * 1024):
                mem_val = round(mem, 2) if mem else "sem agente"
                if not mem or mem < 10.0:
                    custo_est = round(get_ec2_cost(data['type']), 2)
                    idle.append([account_name, 'EC2', data['name'], data['id'], data['type'], round(cpu, 2), mem_val, round(net / 1048576, 2), 'Ociosa', f"${custo_est}"])

    # 2. Volumes EBS Desanexados
    print(f"--- MAPEANDO DISCOS EBS ---")
    for vol in ec2_client.describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}]).get('Volumes', []):
        if vol['CreateTime'] > threshold_date_60: continue
        size_gb = vol.get('Size', 0)
        name = next((t['Value'] for t in vol.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
        
        custo_ebs = round(size_gb * 0.10, 2)
        idle.append([account_name, 'EBS', name, vol['VolumeId'], f"{size_gb} GB ({vol.get('VolumeType', 'gp2')})", '-', '-', '-', 'Desanexado', f"${custo_ebs}"])

    # 3. Snapshots Manuais (+ Antigos que Fevereiro 2026)
    print(f"--- MAPEANDO SNAPSHOTS ANTIGOS ---")
    try:
        snaps = ec2_client.describe_snapshots(OwnerIds=['self']).get('Snapshots', [])
        for snap in snaps:
            if snap['StartTime'] < limit_snapshot_date:
                desc = snap.get('Description', '').lower()
                if 'createimage' not in desc and 'copied for destinationami' not in desc:
                    name = next((t['Value'] for t in snap.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
                    size_gb = snap.get('VolumeSize', 0)
                    custo_sp = round(size_gb * 0.05, 2)
                    idle.append([account_name, 'Snapshot', name, snap['SnapshotId'], f"{size_gb} GB", '-', '-', '-', 'Pré Fev/26', f"${custo_sp}"])
    except Exception as e:
        print(f"Erro ao listar Snapshots: {e}")

    # 4. Elastic IPS (Soltos)
    print(f"--- MAPEANDO ELASTIC IPs ---")
    for eip in [e for e in ec2_client.describe_addresses().get('Addresses', []) if 'AssociationId' not in e]:
        name = next((t['Value'] for t in eip.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
        idle.append([account_name, 'EIP', name, eip['PublicIp'], 'IPv4', '-', '-', '-', 'Solto', "$3.60"])

    # ================= CLOUDWATCH 30 DIAS =================
    queries_30, rds_data, nat_data, ddb_meta, cache_meta = [], {}, {}, {}, {}
    
    # 5. RDS Bancos de Dados
    print(f"--- MAPEANDO BANCOS RDS ---")
    try:
        for db in rds_client.describe_db_instances().get('DBInstances', []):
            if db['DBInstanceStatus'] in ['available', 'stopped']:
                did = db['DBInstanceIdentifier']
                itype = db['DBInstanceClass']
                qid = did.replace('-', '_')
                rds_data[qid] = {'id': did, 'type': itype}
                queries_30.append({'Id': f"rds_{qid}", 'MetricStat': {'Metric': {'Namespace': 'AWS/RDS', 'MetricName': 'DatabaseConnections', 'Dimensions': [{'Name': 'DBInstanceIdentifier', 'Value': did}]}, 'Period': 86400, 'Stat': 'Sum'}, 'ReturnData': True})
    except Exception as e:
        print(f"Erro ao listar RDS: {e}")

    # 6. NAT Gateways
    print(f"--- MAPEANDO NAT GATEWAYS ---")
    try:
        for nat in ec2_client.describe_nat_gateways(Filter=[{'Name': 'state', 'Values': ['available']}]).get('NatGateways', []):
            nid = nat['NatGatewayId']
            qid = nid.replace('-', '_')
            name = next((t['Value'] for t in nat.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
            nat_data[qid] = {'id': nid, 'name': name}
            queries_30.append({'Id': f"nat_{qid}", 'MetricStat': {'Metric': {'Namespace': 'AWS/NATGateway', 'MetricName': 'ActiveConnectionCount', 'Dimensions': [{'Name': 'NatGatewayId', 'Value': nid}]}, 'Period': 86400, 'Stat': 'Sum'}, 'ReturnData': True})
    except Exception as e:
        print(f"Erro ao listar NAT Gateways: {e}")

    # 7. DynamoDB Tabelas Ociosas
    print(f"--- MAPEANDO DYNAMODB ---")
    try:
        tables = ddb_client.list_tables().get('TableNames', [])
        for table in tables:
            desc = ddb_client.describe_table(TableName=table).get('Table', {})
            provisioned = desc.get('ProvisionedThroughput', {})
            rcu = provisioned.get('ReadCapacityUnits', 0)
            wcu = provisioned.get('WriteCapacityUnits', 0)
            billing_mode = desc.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
            
            custo_base = 0.0
            if billing_mode == 'PROVISIONED':
                custo_base = (rcu * 0.09) + (wcu * 0.47)
                
            qid = table.replace('-', '_').replace('.', '_')
            qid = 'ddb_' + ''.join(c for c in qid if c.isalnum() or c == '_').lower()
            
            ddb_meta[qid] = {'name': table, 'rcu': rcu, 'wcu': wcu, 'custo': custo_base}
            
            queries_30.append({
                'Id': f"r_{qid}",
                'MetricStat': {
                    'Metric': {
                        'Namespace': 'AWS/DynamoDB',
                        'MetricName': 'ConsumedReadCapacityUnits',
                        'Dimensions': [{'Name': 'TableName', 'Value': table}]
                    },
                    'Period': 86400,
                    'Stat': 'Sum'
                },
                'ReturnData': True
            })
            queries_30.append({
                'Id': f"w_{qid}",
                'MetricStat': {
                    'Metric': {
                        'Namespace': 'AWS/DynamoDB',
                        'MetricName': 'ConsumedWriteCapacityUnits',
                        'Dimensions': [{'Name': 'TableName', 'Value': table}]
                    },
                    'Period': 86400,
                    'Stat': 'Sum'
                },
                'ReturnData': True
            })
    except Exception as e:
        print(f"Erro ao listar DynamoDB: {e}")

    # 8. ElastiCache Clusters Ociosos
    print(f"--- MAPEANDO ELASTICACHE ---")
    try:
        for cluster in cache_client.describe_cache_clusters(ShowCacheNodeInfo=False).get('CacheClusters', []):
            cid = cluster['CacheClusterId']
            if cluster['CacheClusterStatus'] == 'available':
                node_type = cluster['CacheNodeType']
                num_nodes = cluster.get('NumCacheNodes', 1)
                engine = cluster.get('Engine', 'redis')
                qid = cid.replace('-', '_').replace('.', '_')
                qid = 'ec_' + ''.join(c for c in qid if c.isalnum() or c == '_').lower()
                
                price_per_node = CACHE_PRICES_MONTHLY.get(node_type, 30.0)
                custo_base = price_per_node * num_nodes
                
                cache_meta[qid] = {'id': cid, 'type': node_type, 'nodes': num_nodes, 'custo': custo_base, 'engine': engine}
                
                queries_30.append({
                    'Id': qid,
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/ElastiCache',
                            'MetricName': 'CurrConnections',
                            'Dimensions': [{'Name': 'CacheClusterId', 'Value': cid}]
                        },
                        'Period': 86400,
                        'Stat': 'Average'
                    },
                    'ReturnData': True
                })
    except Exception as e:
        print(f"Erro ao listar ElastiCache: {e}")

    # Processamento Final das metricas de RDS, NAT, DynamoDB e ElastiCache
    if queries_30:
        metrics_30 = fetch_cw_metrics(cw_client, queries_30, start_time_30, end_time)
        for qid, data in rds_data.items():
            conn_avg = metrics_30.get(f"rds_{qid}", 0.0)
            if conn_avg < 1.0:
                custo = round(get_ec2_cost(data['type']), 2) 
                idle.append([account_name, 'RDS DB', data['id'], data['id'], data['type'], '-', '-', '-', 'Sem Conexões (30d)', f"${custo}"])
                
        for qid, data in nat_data.items():
            conn_avg = metrics_30.get(f"nat_{qid}", 0.0)
            if conn_avg < 1.0:
                idle.append([account_name, 'NAT Gateway', data['name'], data['id'], '-', '-', '-', '-', 'Sem Ocupação (30d)', "$32.40"])

        for qid, meta in ddb_meta.items():
            r_sum = metrics_30.get(f"r_{qid}", 0.0)
            w_sum = metrics_30.get(f"w_{qid}", 0.0)
            if r_sum < 0.1 and w_sum < 0.1:
                custo_est = round(meta['custo'], 2)
                detail = f"Provisionado RCU:{meta['rcu']} WCU:{meta['wcu']}" if meta['rcu'] or meta['wcu'] else "Sob Demanda"
                idle.append([account_name, 'DynamoDB', meta['name'], meta['name'], detail, '-', '-', '-', 'Ociosa (30d)', f"${custo_est}"])

        for qid, meta in cache_meta.items():
            conn_avg = metrics_30.get(qid, 0.0)
            if conn_avg < 1.0:
                custo_est = round(meta['custo'], 2)
                detail = f"{meta['nodes']}x {meta['type']} ({meta['engine']})"
                idle.append([account_name, 'ElastiCache', meta['id'], meta['id'], detail, '-', '-', '-', 'Sem Conexões (30d)', f"${custo_est}"])

    # 9. Load Balancers (ALB e NLB) vazios
    print(f"--- MAPEANDO LOAD BALANCERS ---")
    try:
        for lb in elbv2_client.describe_load_balancers().get('LoadBalancers', []):
            arn = lb['LoadBalancerArn']
            lb_name = lb['LoadBalancerName']
            lb_type = lb['Type']
            
            tgs = elbv2_client.describe_target_groups(LoadBalancerArn=arn).get('TargetGroups', [])
            has_targets = False
            for tg in tgs:
                health = elbv2_client.describe_target_health(TargetGroupArn=tg['TargetGroupArn']).get('TargetHealthDescriptions', [])
                if len(health) > 0:
                    has_targets = True
                    break
                    
            if not has_targets:
                idle.append([account_name, 'Load Balancer', lb_name, arn.split('/')[-1], lb_type, '-', '-', '-', 'Zero Targets', "$22.00"])
    except Exception as e:
        print(f"Erro ao listar Load Balancers: {e}")

    # 10. CloudWatch Logs sem Retenção
    print(f"--- MAPEANDO CLOUDWATCH LOGS ---")
    try:
        paginator = logs_client.get_paginator('describe_log_groups')
        for page in paginator.paginate():
            for lg in page.get('logGroups', []):
                if 'retentionInDays' not in lg:
                    lg_name = lg['logGroupName']
                    stored_bytes = lg.get('storedBytes', 0)
                    stored_gb = stored_bytes / (1024 ** 3)
                    custo_logs = round(stored_gb * 0.03, 2)
                    detail = f"{round(stored_gb, 2)} GB (Sem Retenção)"
                    idle.append([account_name, 'Log Group', lg_name, lg_name, detail, '-', '-', '-', 'Sem Retenção', f"${custo_logs}"])
    except Exception as e:
        print(f"Erro ao listar CloudWatch Logs: {e}")

    # 11. AMIs Órfãs (Imagens não utilizadas)
    print(f"--- MAPEANDO AMIs ÓRFÃS ---")
    try:
        used_amis = set()
        try:
            all_insts = [i for r in ec2_client.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['pending', 'running', 'shutting-down', 'stopping', 'stopped']}]).get('Reservations', []) for i in r.get('Instances', [])]
            used_amis = {inst['ImageId'] for inst in all_insts if 'ImageId' in inst}
        except Exception as e_inst:
            print(f"Erro ao obter instâncias para AMIs: {e_inst}")

        try:
            lts = ec2_client.describe_launch_templates().get('LaunchTemplates', [])
            for lt in lts:
                lt_id = lt['LaunchTemplateId']
                versions = ec2_client.describe_launch_template_versions(LaunchTemplateId=lt_id, Versions=['$Default', '$Latest']).get('LaunchTemplateVersions', [])
                for v in versions:
                    ami_id = v.get('LaunchTemplateData', {}).get('ImageId')
                    if ami_id:
                        used_amis.add(ami_id)
        except Exception as e_lt:
            print(f"Erro ao obter Launch Templates para AMIs: {e_lt}")

        my_images = ec2_client.describe_images(Owners=['self']).get('Images', [])
        for img in my_images:
            img_id = img['ImageId']
            if img_id not in used_amis:
                img_name = img.get('Name', 'Sem Nome')
                total_snap_size = 0
                for block in img.get('BlockDeviceMappings', []):
                    if 'Ebs' in block and 'SnapshotId' in block['Ebs']:
                        total_snap_size += block['Ebs'].get('VolumeSize', 0)
                custo_ami = round(total_snap_size * 0.05, 2)
                idle.append([account_name, 'AMI', img_name, img_id, f"Snapshot: {total_snap_size} GB", '-', '-', '-', 'Órfã (Não Utilizada)', f"${custo_ami}"])
    except Exception as e:
        print(f"Erro ao listar AMIs: {e}")

    # 12. Conexões VPN desligadas
    print(f"--- MAPEANDO VPN CONNECTIONS ---")
    try:
        vpns = ec2_client.describe_vpn_connections().get('VpnConnections', [])
        for vpn in vpns:
            if vpn['State'] == 'available':
                vpn_id = vpn['VpnConnectionId']
                name = next((t['Value'] for t in vpn.get('Tags', []) if t['Key'] == 'Name'), "Sem Nome")
                telemetry = vpn.get('VgwTelemetry', [])
                if telemetry and all(t.get('Status') == 'DOWN' for t in telemetry):
                    idle.append([account_name, 'VPN Connection', name, vpn_id, 'Site-to-Site', '-', '-', '-', 'Desconectada (Down)', "$36.00"])
    except Exception as e:
        print(f"Erro ao listar VPN Connections: {e}")

    return idle

def get_all_accounts():
    org = boto3.client('organizations')
    accounts = []
    try:
        for page in org.get_paginator('list_accounts').paginate():
            for account in page['Accounts']:
                if account['Status'] == 'ACTIVE':
                    accounts.append({'Id': account['Id'], 'Name': account['Name']})
        return accounts
    except Exception as e:
        print(f"Aviso: Não listou Orgs. Erro: {e}")
        sts = boto3.client('sts')
        return [{'Id': sts.get_caller_identity()['Account'], 'Name': 'Conta Local'}]

def main():
    print("Iniciando varredura profunda FinOps de ociosidades na AWS...")
    
    s3_client_main = boto3.client('s3', region_name=REGION)
    create_s3_bucket(s3_client_main, BUCKET_NAME, REGION)
    
    accounts = get_all_accounts()
    print(f"[{len(accounts)}] Conta(s) mapeadas com sucesso.")
    all_idle = []
    
    for account_obj in accounts:
        account_id = account_obj['Id']
        account_name = account_obj['Name']
        
        print(f"\n==============================================")
        print(f"PROCESSANDO CONTA: {account_name} ({account_id})")
        print(f"==============================================")
        
        creds = assume_role(account_id, ROLE_NAME)
        
        if creds:
            # Cria clientes repassando as credenciais assumidas da conta
            kargs = {
                'region_name': REGION,
                'aws_access_key_id': creds['AccessKeyId'],
                'aws_secret_access_key': creds['SecretAccessKey'],
                'aws_session_token': creds['SessionToken']
            }
            c_ec2 = boto3.client('ec2', **kargs)
            c_cw = boto3.client('cloudwatch', **kargs)
            c_rds = boto3.client('rds', **kargs)
            c_elb = boto3.client('elbv2', **kargs)
            c_logs = boto3.client('logs', **kargs)
            c_ddb = boto3.client('dynamodb', **kargs)
            c_cache = boto3.client('elasticache', **kargs)
        else:
            print(f"Falha de Credencial. Usando fallback (Credenciais Mestres)!")
            c_ec2 = boto3.client('ec2', region_name=REGION)
            c_cw = boto3.client('cloudwatch', region_name=REGION)
            c_rds = boto3.client('rds', region_name=REGION)
            c_elb = boto3.client('elbv2', region_name=REGION)
            c_logs = boto3.client('logs', region_name=REGION)
            c_ddb = boto3.client('dynamodb', region_name=REGION)
            c_cache = boto3.client('elasticache', region_name=REGION)
            
        # Puxa informações da conta rodando 12 módulos simultâneos 
        all_idle.extend(extract_idle_resources(c_ec2, c_cw, c_rds, c_elb, c_logs, c_ddb, c_cache, account_name))
        
    print("\n--- FINALIZANDO ---")
    if all_idle:
        file_name = f"recursos_superociosos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        with open(file_name, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Conta_Nome', 'Tipo', 'Nome', 'ID', 'Detalhe(Tamanho/Tipo)', 'CPU(%)', 'Mem(%)', 'Rede_Diaria(MB)', 'Status', 'Custo_Mensal_Estimado($)'])
            writer.writerows(all_idle)
            
        print(f"Relatório '{file_name}' gravado.")
        try:
            print(f"Sincronizando '{file_name}' com S3 {BUCKET_NAME}...")
            s3_client_main.upload_file(file_name, BUCKET_NAME, file_name)
            print("Backup de Segurança e Sincronização concluída!")
        except Exception as e:
            print(f"Sincronismo do S3 falhou: {e}")
    else:
        print("Tudo limpo! Sua arquitetura está invejável sem nenhuma ociosidade.")

if __name__ == '__main__':
    main()
