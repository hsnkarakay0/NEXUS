import ssl, getpass, json, re, time, queue, logging, threading, socket
from pathlib import Path
import numpy as np
import sounddevice as sd
import pyttsx3
import requests
from faster_whisper import WhisperModel
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl
from http.client import HTTPException
from xml.parsers.expat import ExpatError
import atexit, os
API_ERRORS=(OSError, HTTPException, ExpatError, vim.fault.NotAuthenticated)
# SmartConnect discovery does not consistently honor its HTTP timeout.
socket.setdefaulttimeout(25)
BASE = Path(__file__).resolve().parent
HOST = os.environ.get('NEXUS_ESXI_HOST') or input('ESXi host: ').strip()
logging.basicConfig(filename=BASE/'nexus.log', level=logging.INFO, format='%(asctime)s %(message)s', encoding='utf-8')
speech_queue = queue.Queue()
def speech_worker():
    try:
        import comtypes
        comtypes.CoInitialize()
        engine=pyttsx3.init(); engine.setProperty('rate',190)
        for voice in engine.getProperty('voices'):
            if any(x in (voice.name+' '+voice.id).lower() for x in ['turkish','tolga','tr-tr']):
                engine.setProperty('voice',voice.id); break
        while True:
            text=speech_queue.get()
            try:
                if text is None: return
                engine.say(text); engine.runAndWait()
            except Exception: logging.exception('TTS')
            finally: speech_queue.task_done()
    except Exception:
        logging.exception('TTS initialization')
        while True:
            text=speech_queue.get(); speech_queue.task_done()
            if text is None: return

def speak(text):
    print('[NEXUS]',text,flush=True); speech_queue.put(text)

model = None
model_lock=threading.Lock()
def get_model():
    global model
    with model_lock:
        if model is None: model=WhisperModel('small',device='cpu',compute_type='int8',download_root=str(BASE/'models'/'whisper'),local_files_only=True,cpu_threads=6)
        return model
def warm_models():
    try: get_model()
    except Exception: logging.exception('Whisper preload')
    try: requests.post('http://127.0.0.1:11434/api/generate',json={'model':'nexus','keep_alive':'30m'},timeout=90).raise_for_status()
    except Exception: logging.warning('Ollama preload failed',exc_info=True)
def listen():
    global model
    speech_queue.join()
    print('[NEXUS] Dinliyorum... (konusma bitince otomatik durur)',flush=True)
    chunks=[]; started=False; quiet=0; threshold=0.0025
    with sd.InputStream(samplerate=16000,channels=1,dtype='float32',device=None,blocksize=1600) as stream:
        for i in range(200):
            data,_=stream.read(1600); chunks.append(data.copy())
            loud=float(np.sqrt(np.mean(data**2)))>threshold
            if loud: started=True; quiet=0
            elif started: quiet+=1
            if started and quiet>=6: break
            if not started and i>=79: print('[MIC] Ses algilanamadi. Mikrofon seviyesini kontrol edin.',flush=True); return ''
    if not started: return ''
    segments,_=get_model().transcribe(np.concatenate(chunks).flatten(),language='tr',beam_size=1,best_of=1,vad_filter=True,condition_on_previous_text=False,initial_prompt='Nexus, VMware, ESXi, sanal makine, sunucu, RAM, CPU, datastore, ISO.')
    return ' '.join(x.text.strip() for x in segments).strip()

def objects(content, kind):
    view = content.viewManager.CreateContainerView(content.rootFolder, [kind], True)
    try: return list(view.view)
    finally: view.Destroy()

def bulk(c, kind, paths):
    pc=c.propertyCollector
    view=c.viewManager.CreateContainerView(c.rootFolder,[kind],True)
    try:
        traversal=vmodl.query.PropertyCollector.TraversalSpec(name='view',type=vim.view.ContainerView,path='view',skip=False)
        spec=vmodl.query.PropertyCollector.FilterSpec(objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=view,skip=True,selectSet=[traversal])],propSet=[vmodl.query.PropertyCollector.PropertySpec(type=kind,all=False,pathSet=paths)])
        result=pc.RetrievePropertiesEx(specSet=[spec],options=vmodl.query.PropertyCollector.RetrieveOptions())
        rows=[]
        while result:
            rows.extend((r.obj,{p.name:p.val for p in r.propSet}) for r in (result.objects or []))
            if not result.token: break
            result=pc.ContinueRetrievePropertiesEx(token=result.token)
        return rows
    finally:
        try: view.Destroy()
        except Exception: logging.warning('View cleanup failed',exc_info=True)

def inventory(c):
    kinds=[(vim.VirtualMachine,['name','runtime.powerState','config.hardware.numCPU','config.hardware.memoryMB']),(vim.Datastore,['name','summary.freeSpace']),(vim.HostSystem,['name','hardware.cpuInfo.numCpuCores','hardware.memorySize','summary.quickStats.overallMemoryUsage'])]
    pc=c.propertyCollector
    view=c.viewManager.CreateContainerView(c.rootFolder,[k for k,_ in kinds],True)
    groups={k:[] for k,_ in kinds}
    try:
        tr=vmodl.query.PropertyCollector.TraversalSpec(name='view',type=vim.view.ContainerView,path='view',skip=False)
        spec=vmodl.query.PropertyCollector.FilterSpec(objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=view,skip=True,selectSet=[tr])],propSet=[vmodl.query.PropertyCollector.PropertySpec(type=k,all=False,pathSet=paths) for k,paths in kinds])
        result=pc.RetrievePropertiesEx(specSet=[spec],options=vmodl.query.PropertyCollector.RetrieveOptions())
        while result:
            for row in result.objects or []:
                for k,_ in kinds:
                    if isinstance(row.obj,k): groups[k].append((row.obj,{p.name:p.val for p in row.propSet or []})); break
            if not result.token: break
            result=pc.ContinueRetrievePropertiesEx(token=result.token)
    finally:
        try: view.Destroy()
        except Exception: logging.warning('View cleanup failed',exc_info=True)
    vms,ds,hosts=(groups[k] for k,_ in kinds)
    return {'vms':[{'name':r.get('name'),'state':str(r.get('runtime.powerState','unknown')),'cpu':r.get('config.hardware.numCPU'),'ram_mb':r.get('config.hardware.memoryMB')} for _,r in vms], 'datastores':[{'name':r.get('name'),'free_gib':round(r['summary.freeSpace']/1024**3,2)} for _,r in ds if 'summary.freeSpace' in r], 'hosts':[{'name':r.get('name'),'cores':r.get('hardware.cpuInfo.numCpuCores'),'ram_gib':round(r['hardware.memorySize']/1024**3,2) if 'hardware.memorySize' in r else None,'used_ram_mb':r.get('summary.quickStats.overallMemoryUsage')} for _,r in hosts]}

def ask(text, state):
    prompt = '''Sen NEXUS VMware ajanin. Yalniz JSON dondur. Eylemler: chat, status, create_vm, deploy_vm, power_on, list_iso, exit. JSON alanlari: action, reply, name, cpu, ram_mb, disk_gb, iso. ISO ve ag ile VM olusturma isteginde deploy_vm sec; iso alanina istenen ISO adini veya Windows Server gibi arama ifadesini yaz. ISO listeleme isteginde list_iso sec. deploy_vm icin de isim CPU RAM disk zorunlu. Yalniz acik VM olusturma isteginde create_vm sec. Isim, CPU, RAM veya disk eksikse chat ile sor; tahmin etme. Yalniz acik VM baslatma isteginde power_on sec. Sunucu kapatma istegi exit degildir; desteklenmedigini bildir. exit yalniz ajani kapatma isteginde. Islem yapildigini iddia etme. Turkce cevap ver. Chat yanitinda altyapi sayilari veya IP verme; durum ve kapasite sorularinda status sec. Canli veri: '''+json.dumps(state,ensure_ascii=False)+'\nKullanici: '+text
    r=requests.post('http://127.0.0.1:11434/api/generate',json={'model':'nexus','prompt':prompt,'format':'json','stream':False,'options':{'temperature':0,'num_predict':100,'num_ctx':2048},'keep_alive':'30m'},timeout=30)
    r.raise_for_status(); result=json.loads(r.json()['response'])
    if not isinstance(result,dict): raise ValueError('Gecersiz model yaniti')
    return result

def wait(task):
    deadline=time.monotonic()+120
    while True:
        info=task.info
        if info.state=='success': return
        if info.state=='error': raise RuntimeError(str(info.error))
        if time.monotonic()>=deadline: raise TimeoutError('VMware gorevi zaman asimina ugradi; sonuc belirsiz.')
        time.sleep(1)

def confirm(plan):
    speak(plan+' Onay icin terminale EVET yazin.')
    return input('ONAY: ').strip().upper()=='EVET'

def iso_catalog(c):
    ds=next(d for d,r in bulk(c,vim.Datastore,['name']) if r.get('name')=='datastore2')
    spec=vim.host.DatastoreBrowser.SearchSpec(matchPattern=['*.iso','*.ISO'])
    task=ds.browser.SearchDatastore_Task(datastorePath='[datastore2] .iso',searchSpec=spec)
    wait(task)
    return sorted({r.folderPath.rstrip('/')+'/'+f.path for r in [task.info.result] for f in (r.file or []) if f.path.lower().endswith('.iso')})

def choose(items,label):
    if not items: raise ValueError(label+' bulunamadi')
    for i,item in enumerate(items,1): print(f'{i}. {item}')
    choice=input(label+' numarasi (iptal: ENTER): ').strip()
    if not choice.isdecimal() or not 1<=int(choice)<=len(items): raise ValueError('Secim iptal veya gecersiz')
    return items[int(choice)-1]

def create(c,p,deploy=False):
    name=p.get('name'); cpu=p.get('cpu'); ram=p.get('ram_mb'); disk=p.get('disk_gb')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}',name): raise ValueError('Gecersiz VM adi')
    if any(type(x) is not int for x in (cpu,ram,disk)) or not (1<=cpu<=4 and 512<=ram<=8192 and 10<=disk<=100): raise ValueError('Kaynaklar izin verilen aralik disinda: CPU 1-4, RAM 512-8192 MB, disk 10-100 GB')
    if any(str(r.get('name','')).casefold()==name.casefold() for _,r in bulk(c,vim.VirtualMachine,['name'])): raise ValueError('VM zaten mevcut')
    host=objects(c,vim.HostSystem)[0]; dc=objects(c,vim.Datacenter)[0]; ds=next(d for d in objects(c,vim.Datastore) if d.name=='datastore1')
    if ds.summary.freeSpace < (disk+5)*1024**3: raise ValueError('Datastore bos alani yetersiz')
    ctl=vim.vm.device.VirtualLsiLogicSASController(key=-100,busNumber=0,sharedBus='noSharing')
    d=vim.vm.device.VirtualDisk(key=-101,controllerKey=-100,unitNumber=0,capacityInKB=disk*1024*1024,backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName='[datastore1]',diskMode='persistent',thinProvisioned=True))
    changes=[vim.vm.device.VirtualDeviceSpec(operation='add',device=ctl),vim.vm.device.VirtualDeviceSpec(operation='add',fileOperation='create',device=d)]
    guest='otherGuest64'
    if deploy:
        iso=p['selected_iso']; netname=p['selected_network']
        matches=[n for n in host.network if n.name==netname and not isinstance(n,vim.dvs.DistributedVirtualPortgroup)]
        if len(matches)!=1: raise ValueError('Ag secimi belirsiz')
        if re.search(r'windows.*server.*2016|server2016',iso,re.I): guest='windows9Server64Guest'
        used=host.summary.quickStats.overallMemoryUsage
        if used is None or host.hardware.memorySize/1024**2-used < ram+1024: raise ValueError('Fiziksel RAM yetersiz')
        ide=vim.vm.device.VirtualAHCIController(key=-102,busNumber=0)
        cd=vim.vm.device.VirtualCdrom(key=-103,controllerKey=-102,unitNumber=0,backing=vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=iso,datastore=next(d for d in host.datastore if d.name=='datastore2')),connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True,allowGuestControl=True,connected=False))
        nic=vim.vm.device.VirtualE1000e(key=-104,addressType='generated',backing=vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=netname,network=matches[0]),connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True,allowGuestControl=True,connected=False))
        changes.extend(vim.vm.device.VirtualDeviceSpec(operation='add',device=x) for x in (ide,cd,nic))
    spec=vim.vm.ConfigSpec(name=name,guestId=guest,version='vmx-14',numCPUs=cpu,memoryMB=ram,files=vim.vm.FileInfo(vmPathName='[datastore1]'),deviceChange=changes)
    p['mutation_started']=True
    task=dc.vmFolder.CreateVM_Task(config=spec,pool=host.parent.resourcePool,host=host)
    wait(task); logging.info('CREATE_VM %s',name)
    if deploy:
        v=task.info.result
        if v is None: raise RuntimeError('VM olusturuldu, ancak sonuc nesnesi alinamadi; paneli kontrol edin')
        devices=v.config.hardware.device
        if not any(isinstance(x,vim.vm.device.VirtualCdrom) and getattr(x.backing,'fileName',None)==iso and x.connectable.startConnected for x in devices): raise RuntimeError('ISO dogrulanamadi; VM kapali birakildi')
        if not any(isinstance(x,vim.vm.device.VirtualEthernetCard) and getattr(x.backing,'deviceName',None)==netname and x.connectable.startConnected for x in devices): raise RuntimeError('Ag dogrulanamadi; VM kapali birakildi')
        used=host.summary.quickStats.overallMemoryUsage
        if used is None or host.hardware.memorySize/1024**2-used < ram+1024: raise ValueError('VM olusturuldu fakat baslatmak icin RAM yetersiz')
        wait(v.PowerOnVM_Task())
        if v.runtime.powerState!='poweredOn': raise RuntimeError('VM baslatma durumu dogrulanamadi')
        logging.info('DEPLOY_VM %s ISO=%s NETWORK=%s',name,iso,netname)
        speak(name+' olusturuldu, ISO ve ag karti dogrulandi, sanal makine baslatildi. Isletim sistemi kurulumu bekleniyor.')
    else: speak(name+' sanal makinesi olusturuldu.')

def power_on(c,p):
    name=p.get('name')
    if not isinstance(name,str): raise ValueError('VM adi gerekli')
    matches=[v for v,r in bulk(c,vim.VirtualMachine,['name']) if str(r.get('name','')).casefold()==name.casefold()]
    if len(matches)!=1: raise ValueError('VM bulunamadi veya isim belirsiz')
    v=matches[0]
    if v.runtime.powerState=='poweredOn': return speak('Sanal makine zaten acik.')
    if v.runtime.powerState!='poweredOff': raise ValueError('VM kapali durumda degil')
    h=v.runtime.host or objects(c,vim.HostSystem)[0]
    used=h.summary.quickStats.overallMemoryUsage
    if used is None or h.hardware.memorySize/1024**2-used < v.config.hardware.memoryMB+1024: raise ValueError('Fiziksel RAM yetersiz veya olculemedi')
    p['mutation_started']=True
    wait(v.PowerOnVM_Task()); logging.info('POWER_ON %s',v.name); speak(v.name+' baslatildi.')

def main():
    print('NEXUS AI v4.1 FAST | VMware LAB',flush=True)
    threading.Thread(target=speech_worker,daemon=True).start()
    threading.Thread(target=warm_models,daemon=True).start()
    user=input('ESXi kullanicisi [root]: ').strip() or 'root'
    password=getpass.getpass('ESXi sifresi: ')
    si=None; content=None; cache=None; cache_time=0
    api_lock=threading.RLock(); stop=threading.Event(); wake=threading.Event(); snapshot={}; last_error=None
    def invalidate():
        nonlocal si,content
        old,si=si,None; content=None
        if old is not None:
            try: old._stub.DropConnections()
            except Exception: pass
    def connect():
        nonlocal si,content
        if si is None or content is None:
            si=SmartConnect(host=HOST,user=user,pwd=password,sslContext=ssl._create_unverified_context() if os.getenv('NEXUS_INSECURE_TLS')=='1' else ssl.create_default_context(),httpConnectionTimeout=25,connectionPoolTimeout=300)
            content=si.RetrieveContent()
        return content
    def read(fn):
        with api_lock:
            for attempt in range(2):
                try: return fn(connect())
                except API_ERRORS:
                    invalidate()
                    if attempt: raise
    def monitor():
        nonlocal snapshot,last_error,cache,cache_time
        while not stop.is_set():
            if api_lock.acquire(blocking=False):
                try:
                    state=inventory(connect())
                    first=not snapshot
                    snapshot={'data':state,'at':time.monotonic()}; last_error=None
                    if first: print('\n[API] Ilk envanter hazir.',flush=True)
                except vim.fault.InvalidLogin as e:
                    last_error=str(e); invalidate(); print('[API] Login rejected; automatic login stopped.',flush=True); stop.set()
                except Exception as e:
                    last_error=str(e); invalidate(); logging.exception('Background inventory'); print('\n[API] Yenileme hatasi:',last_error,flush=True)
                finally: api_lock.release()
            wake.wait(5 if not snapshot else 30); wake.clear()
    def show_status():
        snap=snapshot
        if not snap:
            if last_error: print('[API] Son hata:',last_error,flush=True)
            return speak('Henüz doğrulanmış veri yok. Arka planda bağlantı kuruluyor.')
        age=int(time.monotonic()-snap['at']); state=snap['data']
        print('[ONBELLEK] Son basarili API sorgusu:',age,'saniye once',flush=True)
        if last_error: print('[API] Son yenileme basarisiz:',last_error,flush=True)
        print(json.dumps(state,ensure_ascii=False,indent=2),flush=True)
        speak(f'{age} saniye onceki kayit: {len(state["vms"])} sanal makine, '+str(sum(v['state']=='poweredOn' for v in state['vms']))+' tanesi acik.')
    def catalog(refresh=False):
        nonlocal cache,cache_time
        if refresh or cache is None or time.monotonic()-cache_time>900:
            cache=read(iso_catalog); cache_time=time.monotonic()
        return cache
    def networks(c):
        h=objects(c,vim.HostSystem)[0]
        return sorted({n.name for n in h.network if not isinstance(n,vim.dvs.DistributedVirtualPortgroup) and n.summary.accessible})
    def direct(text):
        parts=text.split(maxsplit=5); cmd=parts[0].lower(); low=text.casefold()
        if cmd=='/exit': return {'action':'exit'}
        if cmd in ('/iso','/refreshiso'): return {'action':'list_iso','refresh':cmd=='/refreshiso'}
        if cmd=='/refresh': wake.set(); return {'action':'status'}
        if cmd=='/status' or re.search(r'(durum|envanter|kapasite|bos alan|boş alan).*(rapor|söyle|göster)|sunucular[ıi]m[ıi]n durum',low): return {'action':'status'}
        if cmd=='/power' and len(parts)==2: return {'action':'power_on','name':parts[1]}
        m=re.search(r'\b([A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)\s+(?:sanal makinesini\s+)?(?:başlat|baslat|aç|ac)\b',text,re.I)
        if m: return {'action':'power_on','name':m.group(1)}
        if cmd in ('/deploy','/create'):
            if len(parts)==1:
                name=input('VM adi (iptal: ENTER): ').strip()
                if not name: return {'action':'chat','reply':'Islem iptal edildi.'}
                cpu=int(input('CPU [2]: ').strip() or '2')
                ram=int(input('RAM MB [4096]: ').strip() or '4096')
                disk=int(input('Disk GB [40]: ').strip() or '40')
                iso=input('ISO tam yolu veya arama ifadesi [server2016.iso]: ').strip() or '[datastore2] .iso/server2016.iso' if cmd=='/deploy' else ''
                return {'action':'deploy_vm' if cmd=='/deploy' else 'create_vm','name':name,'cpu':cpu,'ram_mb':ram,'disk_gb':disk,'iso':iso}
            if len(parts)!=(6 if cmd=='/deploy' else 5): raise ValueError('Kullanim: /deploy AD CPU RAM_MB DISK_GB ISO veya /create AD CPU RAM_MB DISK_GB')
            return {'action':'deploy_vm' if cmd=='/deploy' else 'create_vm','name':parts[1],'cpu':int(parts[2]),'ram_mb':int(parts[3]),'disk_gb':int(parts[4]),'iso':parts[5] if len(parts)==6 else ''}
        if 'kendini tanıt' in low or 'sen kimsin' in low: return {'action':'chat','reply':'Ben Nexus. Yerel yapay zeka destekli VMware altyapi yonetim ajaniyim.'}
        if cmd.startswith('/'): raise ValueError('Bilinmeyen komut')
        return ask(text,{})
    threading.Thread(target=monitor,daemon=True).start()
    speak('Nexus hazir. VMware arka planda kontrol ediliyor.')
    try:
        while True:
            p={}; action=None
            try:
                mode=input('\nENTER: ses | /status | /iso | /deploy | /power | /exit > ').strip()
                text=mode or listen()
                if not text: print('[MIC] Metin algilanamadi.',flush=True); continue
                print('[SEN]',text,flush=True); start=time.monotonic(); p=direct(text); action=p.get('action')
                print('[PLAN]',json.dumps({k:v for k,v in p.items() if k!='reply'},ensure_ascii=False),flush=True)
                if action=='exit': break
                if action=='chat': speak(str(p.get('reply','Komut anlasilamadi.'))[:600]); continue
                if action=='status':
                    show_status(); continue
                if action=='list_iso':
                    paths=catalog(p.get('refresh',False)); print('\n'.join(paths)); speak(f'{len(paths)} ISO bulundu.'); continue
                if action not in ('create_vm','deploy_vm','power_on'): raise ValueError('Desteklenmeyen komut')
                name=p.get('name')
                if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}',name): raise ValueError('Gecersiz VM adi')
                if action!='power_on':
                    cpu,ram,disk=(p.get(k) for k in ('cpu','ram_mb','disk_gb'))
                    if any(type(x) is not int for x in (cpu,ram,disk)) or not (1<=cpu<=4 and 512<=ram<=8192 and 10<=disk<=100): raise ValueError('Gecersiz kaynaklar')
                    plan=f'{name}: {cpu} CPU, {ram} MB RAM, {disk} GB thin disk, datastore1.'
                    if action=='deploy_vm':
                        hint_raw=str(p.get('iso','')).strip(); hint=hint_raw.casefold()
                        if re.fullmatch(r'\[datastore2\] \.iso/[^/\\]+\.iso',hint_raw,re.I):
                            p['selected_iso']=hint_raw
                        else:
                            paths=catalog(); matches=[x for x in paths if hint and hint in x.casefold()]
                            p['selected_iso']=choose(matches if hint else paths,'ISO')
                        p['selected_network']=choose(read(networks),'Standart port group')
                        plan+=f" ISO: {p['selected_iso']}. Ag: {p['selected_network']}. VM baslatilacak."
                else: plan=f'{name} baslatilacak.'
                if not confirm(plan): speak('Islem iptal edildi.'); continue
                # Serialize writes and background reads.
                api_lock.acquire()
                try:
                    for attempt in range(2):
                        try:
                            c=connect()
                            if action=='power_on': power_on(c,p)
                            else: create(c,p,deploy=action=='deploy_vm')
                            break
                        except API_ERRORS:
                            invalidate()
                            if p.get('mutation_started') or attempt==1: raise
                            print('[API] Onayli islem oncesi baglanti yenileniyor...'); time.sleep(attempt+1)
                finally:
                    api_lock.release(); wake.set()
                print(f'[SURE] {time.monotonic()-start:.1f} saniye')
            except Exception as e:
                logging.exception('Islem hatasi'); print('[HATA]',e,flush=True)
                if p.get('mutation_started'):
                    with api_lock: invalidate()
                    try:
                        rows=read(lambda c:[{'name':r.get('name'),'state':str(r.get('runtime.powerState'))} for _,r in bulk(c,vim.VirtualMachine,['name','runtime.powerState']) if str(r.get('name','')).casefold()==str(p.get('name','')).casefold()]); print('[DOGRULAMA]',json.dumps(rows)); speak('Islem sonucu kesinlesmedi. Guncel durum terminalde. Otomatik tekrar yok.')
                    except Exception: speak('Sonuc dogrulanamadi. VMware panelini kontrol edin. Otomatik tekrar yok.')
                else: speak('Islem tamamlanamadi. Terminali kontrol edin.')
    except KeyboardInterrupt: pass
    finally:
        stop.set(); wake.set()
        if api_lock.acquire(blocking=False):
            try: invalidate()
            finally: api_lock.release()
        speech_queue.put(None)
if __name__=='__main__': main()