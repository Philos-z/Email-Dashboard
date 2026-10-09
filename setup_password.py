import getpass, secrets, hashlib, json, os
from pathlib import Path
root=Path(__file__).resolve().parent/'data';root.mkdir(exist_ok=True)
p=root/'auth.json'
if p.exists():
    print('访问密码已经配置。要更改密码，请先停服，然后删除 data/auth.json 并重新运行本工具。')
    raise SystemExit(0)
while True:
    pw=getpass.getpass('请创建 Dashboard 访问密码（至少 12 位）：')
    if len(pw)<12: print('密码至少 12 位，请重试。');continue
    pw2=getpass.getpass('请再输入一次：')
    if pw!=pw2:print('两次不一致，请重试。');continue
    break
salt=secrets.token_bytes(16)
iterations=280000
value={'algorithm':'pbkdf2_sha256','salt':salt.hex(),'iterations':iterations,'hash':hashlib.pbkdf2_hmac('sha256',pw.encode(),salt,iterations).hex()}
fd=os.open(p,os.O_CREAT|os.O_WRONLY|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump(value,f)
print('访问密码已设置，未保存明文。')
