# Auditoria de Ociosidade FinOps (AWS) 🚀

Este repositório contém um script automatizado (em Python) criado para identificar recursos "zumbis" ou ociosos na sua nuvem AWS. O foco principal é gerar visibilidade sobre serviços que estão ligados gerando cobranças desnecessárias (desperdício de dinheiro) em múltiplas contas da organização.

---

## 🕵️ O que o script faz?

Ele se conecta na conta mestre (Management Account) da sua empresa e varre **todas as contas filhas** simultaneamente, procurando por 7 ofensores financeiros principais na AWS:

1. **Instâncias EC2 (Servidores Zumbis):** Máquinas ligadas, mas com uso de CPU, Rede e RAM praticamente zerados aos olhos do CloudWatch nos últimos 90 dias.
2. **Discos EBS (Volumes Desanexados):** Discos de armazenamento que foram esquecidos e não estão conectados a nenhum servidor (Status: *Available*).
3. **Snapshots EBS (Backups Antigos):** Backups de discos manuais criados antes de Fevereiro/2026 que ficaram encalhados cobrando tarifa de retenção na conta (ignorando cópias automatizadas de AMIs da AWS).
4. **Elastic IPs (IPs Públicos Soltos):** IPs reservados na Amazon mas que não estão roteando tráfego ou associados a nenhuma máquina.
5. **Bancos RDS (Bancos Fantasmas):** Bases de dados ligadas ou pausadas que registraram **0 conexões** (0 chamadas) durante no último mês inteiro de faturamento.
6. **NAT Gateways Ociosos:** Gateways caríssimos abandonados sem tráfego de saída no último mês.
7. **Load Balancers (Sem Targets):** Balanceadores em pé cobrando taxa por hora, mas sem nenhum servidor saudável atrelado por trás para receber o tráfego do cliente.

Ao finalizar o rastreio conta a conta, ele junta tudo isso, calcula as estimativas mensais aproximadas de **desperdício em Dólar ($)** de cada um deles e gera um arquivo único (`.csv`), que inclusive é enviado de forma autônoma e segura para um S3 central (*auditoria-ociosidade*) para você nunca perder esse mapeamento!

---

## 📁 Por que esses arquivos existem?

- **`coleta_ociosidade_s3.py`**: É a estrela do show. Esse é o script-fonte em Python que tem todas as regras de negócio das consultas de ociosidade, os limites de datas definidos, os bloqueios de segurança e a lista de geração das tabelas.
- **`requirements.txt`**: É a nossa "lista de compras". Ele avisa para a sua máquina que o script depende exclusivamente da biblioteca `boto3` (o conector/SDK oficial da Amazon) instalada para rodar e conseguir falar com as APIs de nuvem.
- **`.gitignore`**: É um arquivo passivo de segurança de versão. Ele barra o Git e impede você de cometer acidentes enviando credenciais abertas, configurações de máquina ou os relatórios confidenciais expostos (da extensão `.csv`) pro portal online do Github!

---

## 🛠️ Passo a Passo: Como Rodar do Zero (Para Leigos)

Não se assuste se você nunca rodou um script antes. Siga o tutorial abaixo linha a linha utilizando o seu terminal (`Prompt de Comando` (CMD) padrão no Windows).

### Passo 1: Pré-Requisitos (Sua máquina precisa ter)
Para interagir com o código, garanta que você já tem essas duas ferramentas básicas corporativas instaladas na sua máquina Windows:
1. [Python](https://www.python.org/downloads/) (A linguagem nativa que faz o código ser "lido")
2. [AWS CLI](https://aws.amazon.com/cli/) (Painel via terminal da Amazon que cuida da sua autenticação)

### Passo 2: Logando na AWS de forma segura
Você precisa provar que você é da empresa e que possui poderes nativos de "Administrador" na conta AWS para passear pelas permissões. Abra o seu `Prompt de Comando (CMD)` e inicie o assistente de login:
```cmd
aws configure sso
```
**Como preencher a telinha preta que vai surgir:**
1. Cole a `Start URL` interna de acesso ao AWS SSO da sua empresa.
2. Defina a região padrão como `us-east-1` e dê as confirmações.
3. Autorize o popup do navegador que irá se abrir (Click em *Allow Access*).
4. Voltando pra tela preta, navegue no balão flutuante usando as **setinhas do teclado** e Selecione a conta *Master* / Principal (core-rd).
5. Selecione a *Role* (perfil) de *Core-AWSAdministratorAccess*.
6. Formato de Saída (Output): Escreva `json`.
7. **Salvar Perfil (Profile Name):** Escreva ou dê enter para aceitar um apelido (Ex: `sso-rd`), usaremos ele depois.

Sempre que no futuro quiser rodar o script em um dia qualquer e o acesso estiver expirado, basta rodar de forma rápida a re-validação:
```cmd
aws sso login --profile sso-rd
```
*(Caso tenha salvo o profile name só dando enter como "default", ignore o --profile sso-rd no fim).*

### Passo 3: Isolando o Ambiente da Computador (Virtual Env)
Navegue via terminal até a pasta exata em que você salvou este repositório no seu computador (`cd C:\pasta...`).
Para não instalar as livrarias da Amazon misturadas no seu sistema inteiro ou dar quebra de versão do Python com outros projetos da empresa, sempre criamos uma "caixa virtual isolada" (venv).

**Criando a Caixa Virtual (.venv):**
```cmd
python -m venv .venv
```
**Ativando / Ligando a Caixa:**
```cmd
.venv\Scripts\activate.bat
```
*(Nota: Certifique-se de que a escrita `(.venv)` vai aparecer grudada no lado esquerdo do seu terminal após dar enter. Isso significa sucesso!)*

**Lendo a "Lista de Compras" e instalando as bibliotecas faltantes ali dentro:**
```cmd
pip install -r requirements.txt
```

### Passo 4: Rodando a Mágica! 🧙‍♂️
Com o login da Amazon atrelado e o terminal com a caixa `.venv` operante, aponte a setinha do seu ambiente para usar a chave AWS Mestra que pegamos no Passo 2:

*(Preenchendo a Variável Base)*
```cmd
set AWS_PROFILE=sso-rd
```
*(Nota: Se na hora da configuração o profile ficou sendo gigantesco tipo Core-AWSAdministratorAccess-XXXX, apenas confira que copiou integralmente o nome na frente do set AWS_PROFILE).*

E enfim... execute o script!
```cmd
python coleta_ociosidade_s3.py
```

Pronto. Cruze os braços, confie e assista! O terminal começará a cuspir "Mapeando RDS... Mapeando Elastic IPs..." varrendo debaixo pra cima todas as contas sem dar choque no seu navegador. O processo pode levar alguns minutos se a empresa for massiva em nuvem. Logo que for despachado a mensagem de "Backup Sincronizado, Enviado com sucesso!", basta conferir que o Excel limpo aparecerá colado ao lado do seu script. Bom trabalho!
