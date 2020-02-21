# datasets.views
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
#from django.contrib.auth.decorators import user_passes_test
from django.core.files import File
from django.core.paginator import Paginator #,EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.forms import modelformset_factory
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import (
  CreateView, ListView, UpdateView, DeleteView, View, FormView)
from django_celery_results.models import TaskResult

from celery import current_app as celapp
import codecs, tempfile, os, re, sys, math
import simplejson as json
import pandas as pd
from pathlib import Path
from shutil import copyfile
#from itertools import islice
#from pprint import pprint
from areas.models import Area
from main.choices import AUTHORITY_BASEURI
from places.models import *
from datasets.forms import DatasetFileModelForm, HitModelForm, DatasetDetailModelForm, DatasetCreateModelForm
from datasets.models import Dataset, Hit, DatasetFile
from datasets.static.hashes.parents import ccodes
from datasets.tasks import align_tgn, align_whg, align_wd, maxID
from datasets.utils import *
from es.es_utils import makeDoc

def pretty_request(request):
  headers = ''
  for header, value in request.META.items():
    if not header.startswith('HTTP'):
      continue
    header = '-'.join([h.capitalize() for h in header[5:].lower().split('_')])
    headers += '{}: {}\n'.format(header, value)

  return (
      '{method} HTTP/1.1\n'
        'Content-Length: {content_length}\n'
        'Content-Type: {content_type}\n'
        '{headers}\n\n'
        '{body}'
        ).format(
      method=request.method,
        content_length=request.META['CONTENT_LENGTH'],
        content_type=request.META['CONTENT_TYPE'],
        headers=headers,
        body=request.body,
    )
def celeryUp():
  response = celapp.control.ping(timeout=1.0)
  return len(response)>0

def link_uri(auth,id):
  baseuri = AUTHORITY_BASEURI[auth]
  uri = baseuri + str(id)
  return uri

# present reconciliation (and accessioning!) hits for review
# for reconciliation: write place_link & place_geom (if aug_geom == 'on') records if matched
# for accessioning: if close or exact -> if match is parent -> make child else if match is child -> make sibling
def indexMatch(pid, hit_pid=None):
  print('indexMatch, wtf?',pid)
  from elasticsearch import Elasticsearch
  es = Elasticsearch([{'host': 'localhost', 'port': 9200}])
  #idx='whg02'
  idx='whg_test'
  
  if hit_pid == None:
    print('making '+str(pid)+' a parent')
    # TODO:
    whg_id=maxID(es,idx) +1
    place=get_object_or_404(Place,id=pid)
    print('new whg_id',whg_id)
    parent_obj = makeDoc(place,'none')
    parent_obj['relation']={"name":"parent"}
    parent_obj['whg_id']=whg_id
    # add its own names to the suggest field
    for n in parent_obj['names']:
      parent_obj['suggest']['input'].append(n['toponym'])
    # add its title
    if place.title not in parent_obj['suggest']['input']:
      parent_obj['suggest']['input'].append(place.title)
    #index it
    try:
      res = es.index(index=idx, doc_type='place', id=str(whg_id), body=json.dumps(parent_obj))
      #count_seeds +=1
    except:
      #print('failed indexing '+str(place.id), parent_obj)
      print('failed indexing (as parent)'+str(pid))
      pass
    print('created parent:',pid,place.title)    
  else:
    # get _id of hit
    q_hit_pid={"query": {"bool": {"must": [{"match":{"place_id": hit_pid}}]}}}
    res = es.search(index=idx, body=q_hit_pid)
    
    # if hit is a child, get _id of its parent; this will be a sibling 
    # if hit is a parent, get its _id, this will be a child
    if res['hits']['hits'][0]['_source']['relation']['name'] == 'child':
      parent_whgid = res['hits']['hits'][0]['_source']['relation']['parent']
    else:
      parent_whgid = res['hits']['hits'][0]['_id'] #; print(parent_whgid)
    
    # get db record of place, mine its names, make an index doc
    place=get_object_or_404(Place,id=pid)
    match_names = [p.toponym for p in place.names.all()]
    child_obj = makeDoc(place,'none')
    child_obj['relation']={"name":"child","parent":parent_whgid}
    
    # all or nothing; pass if error
    try:
      res = es.index(index=idx,doc_type='place',id=place.id,
                     routing=1,body=json.dumps(child_obj))
      #count_kids +=1                
      print('added '+str(place.id) + ' as child of '+ str(hit_pid))
      # add variants from this record to the parent's suggest.input[] field
      q_update = { "script": {
          "source": "ctx._source.suggest.input.addAll(params.names); ctx._source.children.add(params.id); ctx._source.searchy.addAll(params.names)",
          "lang": "painless",
          "params":{"names": match_names, "id": str(place.id)}
        },
        "query": {"match":{"_id": parent_whgid}}}
      es.update_by_query(index=idx, doc_type='place', body=q_update, conflicts='proceed')
      print('indexed '+str(pid)+' as child of '+str(parent_whgid), child_obj)
    except:
      print('failed indexing '+str(pid)+' as child of '+str(parent_whgid), child_obj)
      #count_fail += 1
      pass
      #sys.exit(sys.exc_info())
  

def isOwner(user):
  task = get_object_or_404(TaskResult, task_id=tid)
  kwargs=json.loads(task.task_kwargs.replace("'",'"'))  
  return kwargs['owner'] == user.id

#@user_passes_test(isOwner)
def review(request, pk, tid, passnum): # dataset pk, celery recon task_id
  #print('review() request', request)
  ds = get_object_or_404(Dataset, id=pk)
  task = get_object_or_404(TaskResult, task_id=tid)
  kwargs=json.loads(task.task_kwargs.replace("'",'"'))
  #print('task_kwargs as json',kwargs)
  # filter place records by passnum for those with unreviewed hits on this task
  cnt_pass = Hit.objects.values('place_id').filter(task_id=tid, reviewed=False, query_pass=passnum).count()
  pass_int = int(passnum[4])
  passnum = passnum if cnt_pass > 0 else 'pass'+str(pass_int+1)
  # [place_id] for places with >0 hits
  
  hitplaces = Hit.objects.values('place_id').filter(
    task_id=tid,
      reviewed=False,
        query_pass=passnum)
  
  if len(hitplaces) > 0:
    record_list = Place.objects.order_by('title').filter(pk__in=hitplaces)
  else:
    context = {"nohits":True,'ds_id':pk,'task_id': tid, 'passnum': passnum}
    return render(request, 'datasets/review.html', context=context)
    # no unreviewed hits

  paginator = Paginator(record_list, 1)
  page = 1 if not request.GET.get('page') else request.GET.get('page')
  records = paginator.get_page(page)
  count = len(record_list)

  placeid = records[0].id
  place = get_object_or_404(Place, id=placeid)
  print('placeid',placeid)

  # recon task hits
  raw_hits = Hit.objects.all().filter(place_id=placeid, task_id=tid).order_by('query_pass','-score')

  # convert ccodes to names
  countries = []
  for r in records[0].ccodes:
  #for r in place.ccodes:
    try:
      countries.append(ccodes[0][r]['gnlabel']+' ('+ccodes[0][r]['tgnlabel']+')')
    except:
      pass

  context = {
    'ds_id':pk, 'ds_label': ds.label, 'task_id': tid,
      'hit_list':raw_hits, 'authority': task.task_name[6:],
        'records': records, 'countries': countries, 'passnum': passnum,
        'page': page if request.method == 'GET' else str(int(page)-1),
        'aug_geom':json.loads(task.task_kwargs.replace("'",'"'))['aug_geom']
  }

  # Hit model fields = ['task_id','authority','dataset','place_id',
  #     'query_pass','src_id','authrecord_id','json','geom' ]
  HitFormset = modelformset_factory(
    Hit,
    fields = ('id','authority','authrecord_id','query_pass','score','json'),
    form=HitModelForm, extra=0)
  formset = HitFormset(request.POST or None, queryset=raw_hits)
  context['formset'] = formset
  #print('context:',context)
  #print('formset data:',formset.data)
  method = request.method
  if method == 'GET':
    print('a GET, just rendering next')
  else:
    try:
      if formset.is_valid():
        # get the task
        hits = formset.cleaned_data
        matches = 0
        for x in range(len(hits)):
          hit = hits[x]['id']
          #if hits[x]['match'] not in ['none','related']:
          if hits[x]['match'] not in ['none']:
            matches += 1
            if task.task_name != 'align_whg':
              # not accessioning; augmenting with links (& geom if requested)
              print('posting links from this hit:',hits[x])
              # if 'accept geometries' was checked in 'Initiate Reconciliation'
              if kwargs['aug_geom'] == 'on' and 'geoms' in hits[x]['json']:
                geom = PlaceGeom.objects.create(
                  place_id = place,
                  task_id = tid,
                  src_id = place.src_id,
                  # {"type": "Point", "geowkt": "POINT(20.58 -19.83)", "citation": {"id": "dplace:SCCS", "label": "Standard cross-cultural sample"}, "coordinates": [20.58, -19.83]}
                  jsonb = {
                    "type":hits[x]['json']['geoms'][0]['type'],
                    "citation":{"id":"wd:"+hits[x]['authrecord_id'],"label":"Wikidata"},
                    "coordinates":hits[x]['json']['geoms'][0]['coordinates']
                  }
                )
              ds.save()

              # create place_link for matched authority record
              print('place_link for', task.task_name,hits[x]['authrecord_id'])
              link = PlaceLink.objects.create(
                place_id = place,
                task_id = tid,
                src_id = place.src_id,
                jsonb = {
                  "type":hits[x]['match'],
                  "identifier":link_uri(task.task_name,hits[x]['authrecord_id'] if hits[x]['authority'] != 'whg' \
                      else hits[x]['json']['place_id'])
                }
              )
              print('place_link', link)
              
              # update <ds>.numlinked, <ds>.total_links
              #ds.numlinked = ds.numlinked +1
              #ds.total_links = ds.total_links +1
              #ds.save()
  
              # grab links in the case of wikidata
              # TODO: check not duplicate
              if 'links' in hits[x]['json']:
                for l in hits[x]['json']['links']:
                  link = PlaceLink.objects.create(
                    place_id = place,
                    task_id = tid,
                    src_id = place.src_id,
                    jsonb = {
                      #"type": re.search("^(.*?):", l).group(1),
                      "type": hits[x]['match'],
                      "identifier": re.search("\: (.*?)$", l).group(1)
                    }
                  )
                  print('posted',link.jsonb)
                  # update totals
                  ds.numlinked = ds.numlinked +1
                  ds.total_links = ds.total_links +1
                  ds.save()
              
            elif task.task_name == 'align_whg':
              # 
              print('see if match for '+str(placeid)+' ('+str(hits[x]['json']['place_id'])+
                    ') is parent or child in index')
              indexMatch(placeid, hits[x]['json']['place_id'])
          elif hits[x]['match'] == 'none' and ds.label in ['gn500','gnmore','tgn_filtered_01']:
            indexMatch(placeid)
            print('made record for:',placeid)
          # flag as reviewed
          matchee = get_object_or_404(Hit, id=hit.id)
          matchee.reviewed = True
          matchee.save()
          #
        if matches == 0:
          print('no matches for',placeid)
          # none are matches, make this place a parent
          #indexMatch(placeid)
          
        return redirect('/datasets/'+str(pk)+'/review/'+tid+'/'+passnum+'?page='+str(int(page)))
      else:
        print('formset is NOT valid')
        #print('formset data:',formset.data)
        print('errors:',formset.errors)
    except:
      sys.exit(sys.exc_info())

  return render(request, 'datasets/review.html', context=context)


# 
# initiate, monitor Celery tasks
def ds_recon(request, pk):
  ds = get_object_or_404(Dataset, id=pk)
  # TODO: handle multipolygons from "#area_load" and "#area_draw"
  me = request.user
  #print('me',me,me.id)
  context = {"dataset": ds.title}

  types_ok=['ccodes','copied','drawn']
  userareas = Area.objects.all().filter(type__in=types_ok).order_by('-created')
  # TODO: this line throws an error but executes !?
  context['area_list'] = userareas if me.username == 'whgadmin' else userareas.filter(owner=me)

  predefined = Area.objects.all().filter(type='predefined').order_by('-created')
  context['region_list'] = predefined

  if request.method == 'GET':
    print('recon request.GET:',request.GET)
  elif request.method == 'POST' and request.POST:
    print('recon request.POST:',request.POST)
    # TODO: has this dataset/authority been done before?
    auth = request.POST['recon']
    # what task?
    func = eval('align_'+auth)
    # TODO: let this vary per authority?
    region = request.POST['region'] # pre-defined UN regions
    userarea = request.POST['userarea'] # from ccodes, loaded, or drawn
    aug_geom = request.POST['geom'] if 'geom' in request.POST else '' # on == write geom if matched
    bounds={
      "type":["region" if region !="0" else "userarea"],
          "id": [region if region !="0" else userarea]
    }
    #print('bounds',bounds)
    
    if not celeryUp():
      print('Celery is down :^(')
      context['response'] = 'snap!'
      context['result'] = "Sorry! The reconciliation task manager is down; working now to get it running"
      return render(request, 'datasets/dataset.html', {'ds':ds, 'context': context})
      
    # run celery/redis tasks e.g. align_tgn, align_wd, align_whg
    try:      
      result = func.delay(
        ds.id,
        ds=ds.id,
          dslabel=ds.label,
          owner=ds.owner.id,
          bounds=bounds,
          aug_geom=aug_geom
      )
    except:
      print(sys.exc_info())
      
    context['hash'] = "#reconciliation"
    context['task_id'] = result.id
    context['response'] = result.state
    context['dataset id'] = ds.label
    context['authority'] = request.POST['recon']
    context['region'] = request.POST['region']
    context['userarea'] = request.POST['userarea']
    context['geom'] = aug_geom
    context['result'] = result.get()
    #print(locals())
    ds.ds_status = 'reconciling'
    ds.save()
    return render(request, 'datasets/dataset.html', {'ds':ds, 'context': context})

  print('context',context)
  return render(request, 'datasets/dataset.html', {'ds':ds, 'context': context})

def task_delete(request,tid,scope="foo"):
  hits = Hit.objects.all().filter(task_id=tid)
  tr = get_object_or_404(TaskResult, task_id=tid)
  ds = tr.task_args[1:-1]

  placelinks = PlaceLink.objects.all().filter(task_id=tid)
  placegeoms = PlaceGeom.objects.all().filter(task_id=tid)

  # zap task record & its hits
  if scope == 'task':
    tr.delete()
    hits.delete()
    placelinks.delete()
    placegeoms.delete()
  elif scope == 'geoms':
    placegeoms.delete()    

  return redirect('/datasets/'+ds+'/detail#reconciliation')
# remove collaborator from dataset
def collab_delete(request,uid,dsid):
  get_object_or_404(DatasetUser,user_id_id=uid,dataset_id_id=dsid).delete()
  return redirect('/datasets/'+str(dsid)+'/detail#sharing')

# add collaborator to dataset
def collab_add(request,dsid,role='member'):
  try:
    uid=get_object_or_404(User,username=request.POST['username']).id
  except:
    # TODO: raise error to screen
    messages.add_message(request, messages.INFO, "Please check username, we don't have '" + request.POST['username']+"'")    
    return redirect('/datasets/'+str(dsid)+'/detail#sharing')
  print('collab_add():',request.POST['username'],dsid,uid)
  DatasetUser.objects.create(user_id_id=uid, dataset_id_id=dsid, role=role)
  return redirect('/datasets/'+str(dsid)+'/detail#sharing')
#
def dataset_browse(request, label, f):
  # need only for title; calls API w/javascript for data
  ds = get_object_or_404(Dataset, label=label)
  filt = f
  return render(request, 'datasets/dataset_browse.html', {'ds':ds,'filter':filt})
# pobj = place object; row is a pandas dict
def add_rels_tsv(pobj, row):
  header = row.keys()
  print('add_rels() from row:',row)
  src_id = row['id']
  title = row['title']
  ccodes = [x.strip() for x in row['ccodes'].split(';')] \
    if 'ccodes' in header else []
  # for PlaceName insertion, strip anything in parens
  title = re.sub('\(.*?\)', '', title)
  title_source = row['title_source']
  title_uri = row['title_uri'] if 'title_uri' in header else ''
  variants = [x.strip() for x in row['variants'].split(';')] \
    if 'variants' in header else []
  types = [x.strip() for x in row['types'].split(';')] \
    if 'types' in header else []
  aat_types = [x.strip() for x in row['aat_types'].split(';')] \
    if 'aat_types' in header else []
  parent_name = row['parent_name'] if 'parent_name' in header else ''
  parent_id = row['parent_id'] if 'parent_id' in header else ''
  coords = makeCoords(row['lon'], row['lat']) \
    if 'lon' in header and 'lat' in header and not math.isnan(row['lon']) else []
  #matches = [x.strip() for x in row['matches')].split(';')] \
    #if 'matches' in header else []
  matches = [x.strip() for x in row['matches'].split(';')] \
    if 'matches' in header and row['matches'] != '' else []
  start = row['start'] if 'start' in header else ''
  end = row['end'] if 'end' in header else ''
  # not sure this will get used
  minmax = [start,end]
  description = row['description'] \
    if 'description' in header else ''

  # build associated objects and add to arrays
  objs = {"PlaceName":[], "PlaceType":[], "PlaceGeom":[], "PlaceWhen":[],
          "PlaceLink":[], "PlaceRelated":[], "PlaceDescription":[],
            "PlaceDepiction":[]}

  # title as a PlaceName
  objs['PlaceName'].append(
    PlaceName(
      place_id=pobj,
      src_id = src_id,
      toponym = title,
      jsonb={"toponym": title, "citation": {"id":title_uri,"label":title_source}}
  ))

  # add variants if any
  if len(variants) > 0:
    for v in variants:
      objs['PlaceName'].append(
        PlaceName(
          place_id=pobj,
          src_id = src_id,
          toponym = v,
          jsonb={"toponym": v, "citation": {"id":"","label":title_source}}
      ))

  # PlaceType()
  # TODO: parse t
  if len(types) > 0:
    for i,t in enumerate(types):
      # i always 0 in tsv
      aatnum=aat_types[i] if len(aat_types) >= len(types) else ''
      objs['PlaceType'].append(
        PlaceType(
          place_id=pobj,
          src_id = src_id,
          jsonb={ "identifier":aatnum,
                  "sourceLabel":t,
                  "label":aat_lookup(int(aatnum)) if aatnum !='' else ''
                }
      ))

  # PlaceGeom()
  # TODO: test geometry type or force geojson
  if len(coords) > 0:
    objs['PlaceGeom'].append(
      PlaceGeom(
        place_id=pobj,
        src_id = src_id,
        jsonb={"type": "Point", "coordinates": coords,
                "geowkt": 'POINT('+str(coords[0])+' '+str(coords[1])+')'}
    ))
  elif 'geowkt' in header and r[header.index('geowkt')] not in ['',None]: # some rows no geom
    objs['PlaceGeom'].append(
      PlaceGeom(
        place_id=pobj,
        src_id = src_id,
        # make GeoJSON using shapely
        jsonb=parse_wkt(r[header.index('geowkt')])
    ))

  # PlaceLink() - all are closeMatch
  if len(matches) > 0:
    countlinked += 1
    for m in matches:
      countlinks += 1
      objs['PlaceLink'].append(
        PlaceLink(
          place_id=pobj,
          src_id = src_id,
          jsonb={"type":"closeMatch", "identifier":m}
      ))

  # PlaceRelated()
  if parent_name != '':
    objs['PlaceRelated'].append(
      PlaceRelated(
        place_id=pobj,
        src_id=src_id,
        jsonb={
          "relationType": "gvp:broaderPartitive",
          "relationTo": parent_id,
          "label": parent_name}
    ))

  # PlaceWhen()
  # timespans[{start{}, end{}}], periods[{name,id}], label, duration
  if start != '':
    objs['PlaceWhen'].append(
      PlaceWhen(
        place_id=pobj,
        src_id = src_id,
        jsonb={
              "timespans": [{"start":{"earliest":minmax[0]}, "end":{"latest":minmax[1]}}]
            }
    ))

  #
  # PlaceDescription()
  # @id, value, lang
  if description != '':
    objs['PlaceDescription'].append(
      PlaceDescription(
        place_id=pobj,
        src_id = src_id,
        jsonb={
          "@id": "", "value":description, "lang":""
        }
      ))

  # what came from this row
  print('COUNTS:')
  print('PlaceName:',len(objs['PlaceName']))
  print('PlaceType:',len(objs['PlaceType']))
  print('PlaceGeom:',len(objs['PlaceGeom']))
  print('PlaceLink:',len(objs['PlaceLink']))
  print('PlaceRelated:',len(objs['PlaceRelated']))
  print('PlaceWhen:',len(objs['PlaceWhen']))
  print('PlaceDescription:',len(objs['PlaceDescription']))
  print('max places.id', )
  
  # bulk_create(Class, batch_size=n) for each
  PlaceName.objects.bulk_create(objs['PlaceName'],batch_size=10000)
  print('names done')
  PlaceType.objects.bulk_create(objs['PlaceType'],batch_size=10000)
  print('types done')
  PlaceGeom.objects.bulk_create(objs['PlaceGeom'],batch_size=10000)
  print('geoms done')
  PlaceLink.objects.bulk_create(objs['PlaceLink'],batch_size=10000)
  print('links done')
  PlaceRelated.objects.bulk_create(objs['PlaceRelated'],batch_size=10000)
  print('related done')
  PlaceWhen.objects.bulk_create(objs['PlaceWhen'],batch_size=10000)
  print('whens done')
  PlaceDescription.objects.bulk_create(objs['PlaceDescription'],batch_size=10000)
  print('descriptions done')
      
#
# perform update on database and index 
def ds_update(request):
  if request.method == 'POST':
    dsid=request.POST['dsid']
    ds = get_object_or_404(Dataset, id=dsid)
    file_format=request.POST['format']
    keepg = request.POST['keepg']
    keepl = request.POST['keepl']

    # compare_data {'compare_result':{}}
    compare_data = json.loads(request.POST['compare_data'])
    compare_result = compare_data['compare_result']
    print('compare_data from ds_compare', compare_data)

    # tempfn has .tsv or .jsonld extension from validation step
    #tempfn = '/var/folders/f4/x09rdl7n3lg7r7gwt1n3wjsr0000gn/T/tmpsuncpnjl.tsv'
    tempfn = compare_data['tempfn']
    filename_new = compare_data['filename_new']
    dsfobj_cur = DatasetFile.objects.filter(dataset_id_id=dsid).order_by('-upload_date')[0]
    rev_cur = dsfobj_cur.rev
    
    # rename file if already exists in user area
    file_exists = Path('media/'+filename_new).exists()
    if file_exists:
      filename_new=filename_new[:-4]+'_'+tempfn[-11:-4]+filename_new[-4:]
        
    # user said go...copy tempfn to media/{user} folder
    filepath = 'media/'+filename_new
    copyfile(tempfn,filepath)    
    
    # and create new DatasetFile instance
    DatasetFile.objects.create(
      dataset_id = ds,
      file = filename_new,
      rev = rev_cur + 1,
      format = file_format,
      # TODO: accept csv, track delimiter
      #delimiter = result['delimiter'] if "delimiter" in result.keys() else "n/a",
      df_status = 'updating',
      upload_date = datetime.date.today(),
      header = compare_result['header_new'],
      numrows = compare_result['count_new']
    )
    
    # (re-)open files as panda dataframes
    if file_format == 'delimited':
      adf = pd.read_csv('media/'+compare_data['filename_cur'], delimiter='\t')
      bdf = pd.read_csv(filepath, delimiter='\t',dtype={'id':'str','ccodes':'str'})
      bdf = bdf.astype({"ccodes": str})
      print('reopened old file, # lines:',len(adf))
      print('reopened new file, # lines:',len(bdf))
      ids_a = adf['id'].tolist()
      ids_b = bdf['id'].tolist()      
      delete_srcids = compare_result['rows_del']      
      replace_srcids = set.intersection(set(ids_b),set(ids_a))
      
      # CURRENT
      # current places
      places = Place.objects.filter(dataset=ds.label)
      # Place.id lists for ES searches
      rows_replace = list(places.filter(src_id__in=replace_srcids).values_list('id',flat=True))
      rows_delete = list(places.filter(src_id__in=delete_srcids).values_list('id',flat=True))
      
      # delete places with ids missing in new data (CASCADE includes links & geoms)
      places.filter(id__in=rows_delete).delete()
      
      # delete related instances for the rest (except links and geoms)
      PlaceName.objects.filter(place_id_id__in=places).delete()
      PlaceType.objects.filter(place_id_id__in=places).delete()
      PlaceWhen.objects.filter(place_id_id__in=places).delete()
      PlaceDescription.objects.filter(place_id_id__in=places).delete()
      PlaceDepiction.objects.filter(place_id_id__in=places).delete()
      
      # augmentation rows have a task_id
      # keep or not is a form choice (keepg, keepl)
      if keepg == 'false':
        # keep none (they are being replaced in update)
        PlaceGeom.objects.filter(place_id_id__in=places).delete()
      else:
        # keep augmentation rows; delete the rest
        PlaceGeom.objects.filter(place_id_id__in=places,task_id=None).delete()
      if keepl == 'false':
        # keep none (they are being replaced in update)
        PlaceLink.objects.filter(place_id_id__in=places).delete()
      else:
        PlaceLink.objects.filter(place_id_id__in=places,task_id=None).delete()
      
      count_updated, count_new = [0,0]
      # update remaining place instances w/data from new file
      place_fields = {'id', 'title', 'ccodes'}
      for index, row in bdf.iterrows():
        # make 3 dicts: all; for Places; for PlaceXxxxs
        #rd=bdf.loc[0].to_dict()
        rd = row.to_dict()
        rdp = {key:rd[key] for key in place_fields}
        #rdrels = {key:rd[key] for key in rd.keys() - place_fields}
        # look for corresponding current place
        #p = places.filter(src_id='1.0').first()
        p = places.filter(src_id=rdp['id']).first()
        if p != None:
          # place exists, update it
          count_updated +=1
          p.title = rdp['title']
          p.ccodes = [] if str(rdp['ccodes']) == 'nan' else rdp['ccodes'].replace(' ','').split(';') 
          p.save()
          #print('updated '+str(p.id)+', add related from '+str(rdrels))
          pobj = p
        else:
          # entirely new place + related records
          count_new +=1
          newpl = Place.objects.create(
            src_id = rdp['id'],
            title = re.sub('\(.*?\)', '', rdp['title']),
            ccodes = rdp['ccodes'].replace(' ','').split(';'),
            dataset = ds
          )
          newpl.save()
          pobj = newpl
          #print('new place, related:', newpl, rdrels)
        
        # create related records (place_name, etc)
        # pobj is either a current (now updated) place or entirely new
        # rd is row dict
        add_rels_tsv(pobj, rd)
    
      result = {"status": "updated", "#updated":count_updated , "#new":count_new
                ,"newfile": filepath, "format":file_format
                }
      return JsonResponse(result,safe=False)


# initial validation and comparison of dataset update files
# ajax call from modal returns json result object
def ds_compare(request):
  if request.method == 'POST':
    print('request.POST',request.POST)
    print('request.FILES',request.FILES)
    dsid=request.POST['dsid']
    user=request.user.username
    format=request.POST['format']
    # moved to ds_update
    #keepg=request.POST['preserve_geoms']
    #keepl=request.POST['preserve_links']
    ds = get_object_or_404(Dataset, id=dsid)
    
    # how many augmenting records previously created? (have task_id)
    count_geoms = PlaceGeom.objects.filter(place_id_id__in=ds.placeids,task_id__isnull=False).count()
    count_links = PlaceLink.objects.filter(place_id_id__in=ds.placeids,task_id__isnull=False).count()
    
    # wrangling names
    # current file
    file_cur = DatasetFile.objects.filter(dataset_id_id=dsid).order_by('-upload_date')[0].file
    filename_cur = file_cur.name
    # new file
    file_new=request.FILES['file']
    tempf, tempfn = tempfile.mkstemp()
    # write new file as temporary
    try:
      for chunk in file_new.chunks():
        os.write(tempf, chunk)
    except:
      raise Exception("Problem with the input file %s" % request.FILES['file'])
    finally:
      os.close(tempf)
    
    print('tempfn,filename_cur,file_new.name',tempfn,filename_cur,file_new.name)
    # open the temp file & send for format validation
    #fin = codecs.open(tempfn, 'r', 'utf8')
    if format == 'delimited':
      #context["format"] = "delimited"
      vresult = goodtable(tempfn)
    elif format == 'lpf':
      # TODO: feed tempfn only, like delimited, and rename
      vresult = validate_lpf(tempfn,'coll')
    print('format, vresult:',format,vresult)
    #fin.close()

    # give new file a path
    filename_new = 'user_'+user+'/'+file_new.name
    # temp files were renamed in validation    
    tempfn_new = tempfn+'.tsv' if format == 'delimited' else tempfn+'.jsonld'
    
    # begin report
    comparison={
      "id": dsid, 
      "filename_cur": filename_cur, 
      "filename_new": filename_new,
      "format": format,
      "validation_result": vresult,
      "tempfn": tempfn_new,
      #"keepg": keepg,
      #"keepl": keepl,
      "count_links": count_links,
      "count_geoms": count_geoms,
    }
    
    # perform comparison
    fn_a = 'media/'+filename_cur
    fn_b = tempfn_new
    if format == 'delimited':
      adf = pd.read_csv(fn_a, delimiter='\t')
      bdf = pd.read_csv(fn_b, delimiter='\t')
      ids_a = adf['id'].tolist()
      ids_b = bdf['id'].tolist()
      # new or removed columns?
      cols_del = list(set(adf.columns)-set(bdf.columns))
      cols_add = list(set(bdf.columns)-set(adf.columns))
      
      comparison['compare_result'] = {
        "count_new":len(ids_b),
        'count_diff':len(ids_b)-len(ids_a),
        'count_replace': len(set.intersection(set(ids_b),set(ids_a))),
        'cols_del': cols_del,
        'cols_add': cols_add,
        'header_new': vresult['columns'],
        'rows_add': [str(x) for x in (set(ids_b)-set(ids_a))],
        'rows_del': [str(x) for x in (set(ids_a)-set(ids_b))]        
      }
    elif format == 'lpf':
      print('need to compare lpf files:',fn_a,fn_b)
      comparison['compare_result'] = "it's lpf...tougher row to hoe"
    
    print('comparison',comparison)
    return JsonResponse(comparison,safe=False)
    #return render(request, 'datasets/dataset.html', {'ds':ds})
#
# insert lpf into database
def ds_insert_lpf(request, pk):
  import json
  [countrows,countlinked]= [0,0]
  ds = get_object_or_404(Dataset, id=pk)
  # insert data from initial file upload
  dsf = DatasetFile.objects.filter(dataset_id_id=pk).order_by('-upload_date')[0]
  uribase = DatasetFile.objects.filter(dataset_id_id=pk).order_by('-upload_date')[0].uri_base

  infile = dsf.file.open(mode="r")
  print('ds_insert_lpf(); request.GET; infile',request.GET,infile)  
  with infile:
    jdata = json.loads(infile.read())
    for feat in jdata['features']:
      #print('feat properties:',feat['properties'])
      objs = {"PlaceNames":[], "PlaceTypes":[], "PlaceGeoms":[], "PlaceWhens":[],
              "PlaceLinks":[], "PlaceRelated":[], "PlaceDescriptions":[],
                "PlaceDepictions":[]}
      countrows += 1
      #
      print(feat['@id'],feat['properties']['title'],feat.keys())
      # TODO: get src_id into LP format

      # start Place record & save to get id
      # Place: src_id, title, ccodes, dataset
      newpl = Place(
        # TODO: add src_id to properties in LP format?
        #src_id=feat['@id'] if 'http' not in feat['@id'] and len(feat['@id']) < 25 \
          #else re.search("(\/|=)(?:.(?!\/|=))+$",feat['@id']).group(0)[1:],
        src_id=feat['@id'] if uribase == None else feat['@id'].replace(uribase,''),
        dataset=ds,
        title=feat['properties']['title'],
        ccodes=feat['properties']['ccodes'] if 'ccodes' in feat['properties'].keys() else []
      )
      newpl.save()

      # PlaceName: place_id,src_id,toponym,task_id,jsonb:{toponym, lang,citation,when{}}
      # TODO: adjust for 'ethnic', 'demonym'
      for n in feat['names']:
        #print('from feat[names]:',n)
        if 'toponym' in n.keys():
          objs['PlaceNames'].append(PlaceName(
            place_id=newpl,
            src_id=newpl.src_id,
            toponym=n['toponym'],
            jsonb=n,
            task_id='initial'
          ))

      # PlaceType: place_id,src_id,task_id,jsonb:{identifier,label,src_label}
      if 'types' in feat.keys():
        for t in feat['types']:
          #print('from feat[types]:',t)
          objs['PlaceTypes'].append(PlaceType(
            place_id=newpl,
            src_id=newpl.src_id,
            jsonb=t
          ))

      # PlaceWhen: place_id,src_id,task_id,minmax,jsonb:{timespans[],periods[],label,duration}
      if 'whens' in feat.keys():
        for w in feat['whens']:
          objs['PlaceWhens'].append(PlaceWhen(
            place_id=newpl,src_id=newpl.src_id,jsonb=w))

      # PlaceGeom: place_id,src_id,task_id,jsonb:{type,coordinates[],when{},geo_wkt,src}
      if 'geometry' in feat.keys():
        for g in feat['geometry']['geometries']:
          #print('from feat[geometry]:',g)
          objs['PlaceGeoms'].append(PlaceGeom(
            place_id=newpl,src_id=newpl.src_id,jsonb=g))

      # PlaceLink: place_id,src_id,task_id,jsonb:{type,identifier}
      if 'links' in feat.keys():
        for l in feat['links']:
          if len(feat['links'])>0: countlinked +=1
          objs['PlaceLinks'].append(PlaceLink(
            place_id=newpl,src_id=newpl.src_id,jsonb=l,task_id='initial'))

      # PlaceRelated: place_id,src_id,task_id,jsonb{relationType,relationTo,label,when{}}
      if 'relations' in feat.keys():
        for r in feat['relations']:
          objs['PlaceRelated'].append(PlaceRelated(
            place_id=newpl,src_id=newpl.src_id,jsonb=r))

      # PlaceDescription: place_id,src_id,task_id,jsonb{@id,value,lang}
      if 'descriptions' in feat.keys():
        for des in feat['descriptions']:
          objs['PlaceDescriptions'].append(PlaceDescription(
            place_id=newpl,src_id=newpl.src_id,jsonb=des))

      # PlaceDepiction: place_id,src_id,task_id,jsonb{@id,title,license}
      if 'depictions' in feat.keys():
        for dep in feat['depictions']:
          objs['PlaceDepictions'].append(PlaceDepiction(
            place_id=newpl,src_id=newpl.src_id,jsonb=dep))

      #print("objs['PlaceNames']",objs['PlaceNames'])
      PlaceName.objects.bulk_create(objs['PlaceNames'])
      PlaceType.objects.bulk_create(objs['PlaceTypes'])
      PlaceWhen.objects.bulk_create(objs['PlaceWhens'])
      PlaceGeom.objects.bulk_create(objs['PlaceGeoms'])
      PlaceLink.objects.bulk_create(objs['PlaceLinks'])
      PlaceRelated.objects.bulk_create(objs['PlaceRelated'])
      PlaceDescription.objects.bulk_create(objs['PlaceDescriptions'])
      PlaceDepiction.objects.bulk_create(objs['PlaceDepictions'])

      #context = {'status':'inserted'}
      # write some summary attributes
      dsf.df_status = 'uploaded'
      dsf.numrows = countrows
      dsf.save()

      ds.ds_status = 'uploaded'
      ds.numrows = countrows
      ds.numlinked = countlinked
      ds.total_links = len(objs['PlaceLinks'])
      ds.save()

    #print('record:', ds.__dict__)
    infile.close()

  print(str(countrows)+' inserted')
  messages.add_message(request, messages.INFO, 'inserted lpf for '+str(countrows)+' places')
  return redirect('/dashboard')

#
# insert LP-TSV file to database
def ds_insert_tsv(request, pk):
  # retrieve just-added file, insert to db
  import os, csv
  ds = get_object_or_404(Dataset, id=pk)
  dsf = DatasetFile.objects.filter(dataset_id_id=pk).order_by('-upload_date')[0]

  infile = dsf.file.open(mode="r")
  print('ds_insert_tsv(); request.GET; infile',request.GET,infile)
  # should already know delimiter
  try:
    dialect = csv.Sniffer().sniff(infile.read(16000),['\t',';','|'])
    reader = csv.reader(infile, dialect)
  except:
    reader = csv.reader(infile, delimiter='\t')
  infile.seek(0)
  header = next(reader, None)
  print('header', header)

  objs = {"PlaceName":[], "PlaceType":[], "PlaceGeom":[], "PlaceWhen":[],
          "PlaceLink":[], "PlaceRelated":[], "PlaceDescription":[],
            "PlaceDepiction":[]}

  # TSV * = required; ^ = encouraged
  # lists within fields are ';' delimited, no brackets
  # id*, title*, title_source*, title_uri^, ccodes[]^, matches[]^, variants[]^, types[]^, aat_types[]^,
  # parent_name, parent_id, lon, lat, geowkt, geo_source, geo_id, start, end
  
  # moved to utils
  #def makeCoords(lonstr,latstr):
    #lon = float(lonstr) if lonstr != '' else ''
    #lat = float(latstr) if latstr != '' else ''
    #coords = [] if (lonstr == ''  or latstr == '') else [lon,lat]
    #return coords
  #
  # TODO: what if simultaneous inserts?
  countrows=0
  countlinked = 0
  countlinks = 0
  for r in reader:
    src_id = r[header.index('id')]
    #print('src_id from tsv_insert',src_id)
    title = r[header.index('title')].replace("' ","'")
    # for PlaceName insertion, strip anything in parens
    title = re.sub('\(.*?\)', '', title)
    title_source = r[header.index('title_source')]
    #print('src_id, title, title_source from tsv_insert',src_id,title,title_source)
    title_uri = r[header.index('title_uri')] if 'title_uri' in header else ''
    variants = [x.strip() for x in r[header.index('variants')].split(';')] \
      if 'variants' in header else []
    types = [x.strip() for x in r[header.index('types')].split(';')] \
      if 'types' in header else []
    aat_types = [x.strip() for x in r[header.index('aat_types')].split(';')] \
      if 'aat_types' in header else []
    #print('types, aat_types',types, aat_types)
    ccodes = [x.strip() for x in r[header.index('ccodes')].split(';')] \
      if 'ccodes' in header else []
    parent_name = r[header.index('parent_name')] if 'parent_name' in header else ''
    parent_id = r[header.index('parent_id')] if 'parent_id' in header else ''
    coords = makeCoords(r[header.index('lon')],r[header.index('lat')]) \
      if 'lon' in header and 'lat' in header else []
    #matches = [x.strip() for x in r[header.index('matches')].split(';')] \
      #if 'matches' in header else []
    matches = [x.strip() for x in r[header.index('matches')].split(';')] \
      if 'matches' in header and r[header.index('matches')] != '' else []
    start = r[header.index('start')] if 'start' in header else ''
    end = r[header.index('end')] if 'end' in header else ''
    # not sure this will get used
    minmax = [start,end]
    description = r[header.index('description')] \
      if 'description' in header else ''

    # build and save Place object
    # id now available as newpl
    newpl = Place(
      src_id = src_id,
      dataset = ds,
      title = title,
      ccodes = ccodes
    )
    newpl.save()
    countrows += 1

    # build associated objects and add to arrays
    # PlaceName()
    objs['PlaceName'].append(
      PlaceName(
        place_id=newpl,
        src_id = src_id,
        toponym = title,
        jsonb={"toponym": title, "citation": {"id":title_uri,"label":title_source}}
    ))

    # variants if any; same source as title toponym?
    if len(variants) > 0:
      for v in variants:
        objs['PlaceName'].append(
          PlaceName(
            place_id=newpl,
            src_id = src_id,
            toponym = v,
            jsonb={"toponym": v, "citation": {"id":"","label":title_source}}
        ))

    # PlaceType()
    # TODO: parse t
    if len(types) > 0:
      for i,t in enumerate(types):
        # i always 0 in tsv
        aatnum=aat_types[i] if len(aat_types) >= len(types) else ''
        objs['PlaceType'].append(
          PlaceType(
            place_id=newpl,
            src_id = src_id,
            jsonb={ "identifier":aatnum,
                    "sourceLabel":t,
                    "label":aat_lookup(int(aatnum)) if aatnum !='' else ''
                  }
        ))

    # PlaceGeom()
    # TODO: test geometry type or force geojson
    if len(coords) > 0:
      objs['PlaceGeom'].append(
        PlaceGeom(
          place_id=newpl,
          src_id = src_id,
          jsonb={"type": "Point", "coordinates": coords,
                      "geowkt": 'POINT('+str(coords[0])+' '+str(coords[1])+')'}
      ))
    elif 'geowkt' in header and r[header.index('geowkt')] not in ['',None]: # some rows no geom
      objs['PlaceGeom'].append(
        PlaceGeom(
          place_id=newpl,
          src_id = src_id,
          # make GeoJSON using shapely
          jsonb=parse_wkt(r[header.index('geowkt')])
      ))

    # PlaceLink() - all are closeMatch
    if len(matches) > 0:
      countlinked += 1
      for m in matches:
        countlinks += 1
        objs['PlaceLink'].append(
          PlaceLink(
            place_id=newpl,
            src_id = src_id,
            jsonb={"type":"closeMatch", "identifier":m}
        ))

    # PlaceRelated()
    if parent_name != '':
      objs['PlaceRelated'].append(
        PlaceRelated(
          place_id=newpl,
          src_id=src_id,
          jsonb={
            "relationType": "gvp:broaderPartitive",
            "relationTo": parent_id,
            "label": parent_name}
      ))

    # PlaceWhen()
    # timespans[{start{}, end{}}], periods[{name,id}], label, duration
    if start != '':
      objs['PlaceWhen'].append(
        PlaceWhen(
          place_id=newpl,
          src_id = src_id,
          jsonb={
                "timespans": [{"start":{"earliest":minmax[0]}, "end":{"latest":minmax[1]}}]
              }
      ))

    #
    # PlaceDescription()
    # @id, value, lang
    if description != '':
      objs['PlaceDescription'].append(
        PlaceDescription(
          place_id=newpl,
          src_id = src_id,
          jsonb={
            "@id": "", "value":description, "lang":""
          }
        ))

  print('COUNTS:')
  print('PlaceName:',len(objs['PlaceName']))
  print('PlaceType:',len(objs['PlaceType']))
  print('PlaceGeom:',len(objs['PlaceGeom']))
  print('PlaceLink:',len(objs['PlaceLink']))
  print('PlaceRelated:',len(objs['PlaceRelated']))
  print('PlaceWhen:',len(objs['PlaceWhen']))
  print('PlaceDescription:',len(objs['PlaceDescription']))
  print('max places.id', )

  # bulk_create(Class, batch_size=n) for each
  PlaceName.objects.bulk_create(objs['PlaceName'],batch_size=10000)
  print('names done')
  PlaceType.objects.bulk_create(objs['PlaceType'],batch_size=10000)
  print('types done')
  PlaceGeom.objects.bulk_create(objs['PlaceGeom'],batch_size=10000)
  print('geoms done')
  PlaceLink.objects.bulk_create(objs['PlaceLink'],batch_size=10000)
  print('links done')
  PlaceRelated.objects.bulk_create(objs['PlaceRelated'],batch_size=10000)
  print('related done')
  PlaceWhen.objects.bulk_create(objs['PlaceWhen'],batch_size=10000)
  print('whens done')
  PlaceDescription.objects.bulk_create(objs['PlaceDescription'],batch_size=10000)
  print('descriptions done')

  infile.close()

  print('ds record pre-update:', ds.__dict__)

  print('rows,linked,links:',countrows,countlinked,countlinks)
  #ds.status = 'uploaded'
  ds.numrows = countrows
  ds.numlinked = countlinked
  ds.total_links = countlinks
  ds.save()
  
  print('ds record post-update:', ds.__dict__)

  #dsf.status = 'uploaded'
  #dsf.numrows = countrows
  #dsf.header = header
  #dsf.save()


  #return render(request, '/datasets/dashboard.html', {'context': context})
  #return redirect('/dashboard', context=context)

#
# list user datasets, area, place collections
class DashboardView(ListView):
  context_object_name = 'dataset_list'
  template_name = 'datasets/dashboard.html'

  def get_queryset(self):
    # TODO: make .team() a method on User
    me = self.request.user
    if me.username in ['whgadmin','karlg']:
      print('in get_queryset() if',me)
      return Dataset.objects.all().order_by('ds_status','-core','-id')
    else:
      # returns permitted datasets (rw) + black and dplace (ro)
      return Dataset.objects.filter( Q(id__in=myprojects(me)) | Q(owner=me) | Q(id__lt=3)).order_by('-id')


  def get_context_data(self, *args, **kwargs):
    me = self.request.user
    context = super(DashboardView, self).get_context_data(*args, **kwargs)
    print('in get_context',me)

    types_ok=['ccodes','copied','drawn']
    # list areas
    userareas = Area.objects.all().filter(type__in=types_ok).order_by('created')
    context['area_list'] = userareas if me.username == 'whgadmin' else userareas.filter(owner=self.request.user)

    context['viewable'] = ['uploaded','inserted','reconciling','review_hits','reviewed','review_whg','indexed']
    # TODO: user place collections
    #print('DashboardView context:', context)
    return context


# initial create
# upload file, validate format, create DatasetFile instance,
# redirect to dataset.html for db insert if context['format_ok']
#
class DatasetCreateView(LoginRequiredMixin, CreateView):
  form_class = DatasetCreateModelForm
  template_name = 'datasets/dataset_create.html'
  success_message = 'dataset created'

  def form_invalid(self,form):
    print('form invalid...',form.errors.as_data())
    context = {'form': form}
    #return self.render_to_response(self.get_context_data(form=form,context=context))
    return self.render_to_response(context=context)
    #return redirect('datasets/dataset_create.html', context)    
      
  def form_valid(self, form):
    data=form.cleaned_data
    context={"format":""}
    user=self.request.user
    file=self.request.FILES['file']
    filename = file.name
    print('form is valid; request',user,filename)
    #TODO: generate a slug label?
    #label = data['title'][:16]+'_'+user.first_name[:1]+user.last_name[:1]
    
    # open & write tempf to a temp location;
    # call it tempfn for reference
    tempf, tempfn = tempfile.mkstemp()
    print('tempfn, filename, file in DatasetCreateView()',tempfn, filename, data['file'])
    try:
      for chunk in data['file'].chunks():
        #print('chunk',chunk)
        os.write(tempf, chunk)
    except:
      raise Exception("Problem with the input file %s" % request.FILES['file'])
    finally:
      os.close(tempf)
      
    # open the temp file
    fin = codecs.open(tempfn, 'r', 'utf8')
    
    # send for format validation
    if data['format'] == 'delimited':
      context["format"] = "delimited"
      result = goodtable(tempfn)
    elif data['format'] == 'lpf':
      context["format"] = "lpf"
      # coll = FeatureCollection
      # TODO: json-lines alternative 
      result = validate_lpf(fin,'coll')
    print('validation result:',context["format"],result)
    fin.close()

    print('validation complete, still in DatasetCreateView')
    
    # create Dataset & DatasetFile instances & advance to dataset_detail if validated
    # otherwise present form again with errors
    if len(result['errors']) == 0:
      context['status'] = 'format_ok'
      print('cleaned_data',form.cleaned_data)
      
      # new Dataset record ('owner','id','label','title','description')
      dsobj = form.save(commit=False)
      dsobj.ds_status = 'format_ok'
      try:
        dsobj.save()
      except:
        args['form'] = form
        return render(request,'datasets/dataset_create.html', args)
        #sys.exit(sys.exc_info())

      # build path, and rename file if already exists in user area
      file_exists = Path('media/user_'+user.username+'/'+filename).exists()
      if not file_exists:
        filepath = 'media/user_'+user.username+'/'+filename
      else:
        filename=filename[:-4]+'_'+tempfn[-7:]+filename[-4:]
        filepath = 'media/user_'+user.username+'/'+filename

      # write the file
      fout = codecs.open(filepath,'w','utf8')
      try:
        for chunk in file.chunks():
          fout.write(chunk.decode("utf-8"))
      except:
        sys.exit(sys.exc_info())
        
      # create initial DatasetFile record
      DatasetFile.objects.create(
        dataset_id = dsobj,
        file = 'user_'+user.username+'/'+filename,
        rev = 1,
        format = result['format'],
        delimiter = result['delimiter'] if "delimiter" in result.keys() else "n/a",
        df_status = 'format_ok',
        upload_date = None,
        header = result['columns'] if "columns" in result.keys() else [],
        numrows = result['count']
      )
      
      # data will be written on load of detail w/dsobj.status = 'format_ok'
      return redirect('/datasets/'+str(dsobj.id)+'/detail')

    else:
      context['ds_status'] = 'format_error'
      context['errors'] = result['errors']
      context['action'] = ''
      result['columns'] if "columns" in result.keys() else []
      print('result:', result)
      return self.render_to_response(self.get_context_data(form=form,context=context))

  def get_context_data(self, *args, **kwargs):
    context = super(DatasetCreateView, self).get_context_data(*args, **kwargs)
    #context['action'] = 'create'
    return context


# dataset summary for "dataset portal" v1
# initiates DataFile update also
class DatasetDetailView(LoginRequiredMixin,UpdateView):
  form_class = DatasetDetailModelForm
  template_name = 'datasets/dataset.html'

  def get_success_url(self):
    id_ = self.kwargs.get("id")
    return '/datasets/'+str(id_)+'/detail'

  # only if submitted
  def form_valid(self, form):
    print('in form_valid()')
    data=form.cleaned_data
    ds = get_object_or_404(Dataset,pk=self.kwargs.get("id"))
    dsid = ds.id
    user = self.request.user
    file=data['file']
    filerev = DatasetFile.objects.filter(dataset_id_id=dsid).order_by('-rev')[0].rev
    print('DatasetDetailView kwargs',self.kwargs)
    print('DatasetDetailView form_valid() data->', data)
    if data["file"] == None:
      print('data["file"] == None')
      # no file, updating dataset only
      ds.title = data['title']
      ds.description = data['description']
      ds.uri_base = data['uri_base']
      ds.save()
    else:
      # a file has been uploaded; need to validate, analyze, & return results
      print('file '+data["file"].name+' has been uploaded; need: validation, analysis, return results')
      context={}
      owner=data['owner'].username
      filename = file.name
      tempf, tempfn = tempfile.mkstemp()
      print('tempfn, filename in DatasetDetailView()',tempfn, filename)
      try:
        for chunk in file.chunks():
          os.write(tempf, chunk)
      except:
        raise Exception("Problem with the input file %s" % file)
      finally:
        os.close(tempf)
        
      # open the temp file
      fin = codecs.open(tempfn, 'r', 'utf8')
      
      # send for format validation
      if data['format'] == 'delimited':
        context['format'] = 'delimited'
        result = goodtable(tempfn)
      elif data['format'] == 'lpf':
        # TODO: json-lines alternative 
        context['format'] = 'lpf'
        result = validate_lpf(fin,'coll')
      print('validation result:',result)
      fin.close()
  
      print('data valid, still in DatasetDetailView')
    
      if len(result['errors']) == 0:
        context['status'] = 'newfile_ok'
        print('validation result',result)

        # create file name
        file_exists = Path('media/user_'+user.username+'/'+filename).exists()
        print('filename at write, exists?',filename,file_exists)
        if not file_exists:
          filepath = 'media/user_'+user.username+'/'+filename
        else:
          filename=filename[:-4]+'_'+tempfn[-7:]+filename[-4:]
          filepath = 'media/user_'+user.username+'/'+filename
  
        print("filepath to open/write",filepath)
        fout = codecs.open(filepath,'w','utf8')
        try:
          for chunk in file.chunks():
            #print('chunk',chunk, type(chunk))
            fout.write(chunk.decode("utf-8"))
        except:
          sys.exit(sys.exc_info())
        finally:
          fout.close()
        
        ds.ds_status = 'updating'
        # add DatasetFile record
        DatasetFile.objects.create(
          dataset_id = ds,
          file = 'user_'+user.username+'/'+filename,
          rev = filerev+1,
          uri_base = data['uri_base'],
          format = result['format'],
          delimiter = result['delimiter'] if "delimiter" in result.keys() else "n/a",
          df_status = 'updating',
          accepted_date = None,
          header = result['columns'] if "columns" in result.keys() else [],
          numrows = result['count']
        )
        
        #comparison=compare(ds.id)
        #print('comparison',comparison)
        # now analyze differences
        #file_a = DatasetFile.objects.filter(dataset_id_id=ds.id).order_by('-rev')[1]
        #file_b = DatasetFile.objects.filter(dataset_id_id=ds.id).order_by('-rev')[0]
        #adf = pd.read_csv('media/'+file_a.file.name,delimiter='\t')
        #bdf = pd.read_csv('media/'+file_b.file.name,delimiter='\t')
        #ids_a = adf['id'].tolist()
        #ids_b = bdf['id'].tolist()
        #astats={"count":len(ids_a)}
        #bstats={"count":len(ids_b)}
        return redirect('/datasets/'+ds+'/detail')
        
        
      else:
        print('validation errors',result['errors'])
        
    return super().form_valid(form)
  
  def form_invalid(self,form):
    print('kwargs',self.kwargs)
    context = {}
    print('form not valid', form.errors)
    print('cleaned_data', form.cleaned_data)
    #cleaned_data {'owner': <User: A_User>, 'label': 'places_p169b', 'title': 'P169 monasteries!', 'description': 'monasteries visited by BDRC P169', 'datatype': 'place', 'uri_base': 'http://library.bdrc.io/show/', 'format': 'delimited'}
    context['errors'] = form.errors
    return super().form_invalid(form)
    
  def get_object(self):
    #print('kwargs:',self.kwargs)
    id_ = self.kwargs.get("id")
    return get_object_or_404(Dataset, id=id_)
  
  def get_context_data(self, *args, **kwargs):
    context = super(DatasetDetailView, self).get_context_data(*args, **kwargs)
    print('DatasetDetailView get_context_data() kwargs:',self.kwargs)
    id_ = self.kwargs.get("id")
    ds = get_object_or_404(Dataset, id=id_)
    file = DatasetFile.objects.filter(dataset_id_id = ds.id).order_by('-upload_date')[0]
    
    # from DatasetCreateView()
    # insert to db immediately if file.df_status == format_ok 
    if file.df_status == 'format_ok':
      print('format_ok , inserting dataset '+str(id_))
      if file.format == 'delimited':
        ds_insert_tsv(self.request, id_)
        #ds_insert_tsv(self.request, ds.id)
        print('numlinked immed. after insert',ds.numlinked)
      else:
        ds_insert_lpf(self.request,id_)
      ds.ds_status = 'uploaded'
      file.df_status = 'uploaded'
      ds.save()
      file.save()

    # load areas for dropdowns
    me = self.request.user
    area_types=['ccodes','copied','drawn']
    
    userareas = Area.objects.all().filter(type__in=area_types).order_by('-created')
    context['area_list'] = userareas if me.username == 'whgadmin' else userareas.filter(owner=me)
  
    predefined = Area.objects.all().filter(type='predefined').order_by('-created')
    context['region_list'] = predefined
  
    #context['comparison'] = comparison
    
    context['updates'] = {}
    bounds = self.kwargs.get("bounds")
    # print('ds',ds.label)
    context['ds'] = ds
    # latest file
    context['current_file'] = file
    context['format'] = file.format
    context['numrows'] = file.numrows
    #context['users'] = ds.dsusers
    context['collab'] = ds.collab
    placeset = Place.objects.filter(dataset=ds.label)
    context['tasks'] = TaskResult.objects.all().filter(task_args = [id_],status='SUCCESS')
    # initial (non-task)
    context['num_links'] = PlaceLink.objects.filter(
      place_id_id__in = placeset, task_id = None).count()
    context['num_names'] = PlaceName.objects.filter(place_id_id__in = placeset).count()
    context['num_geoms'] = PlaceGeom.objects.filter(
      place_id_id__in = placeset, task_id = None).count()
    context['num_descriptions'] = PlaceDescription.objects.filter(
      place_id_id__in = placeset, task_id = None).count()
    # others
    context['num_types'] = PlaceType.objects.filter(
      place_id_id__in = placeset).count()
    context['num_when'] = PlaceWhen.objects.filter(
      place_id_id__in = placeset).count()
    context['num_related'] = PlaceRelated.objects.filter(
      place_id_id__in = placeset).count()
    context['num_depictions'] = PlaceDepiction.objects.filter(
      place_id_id__in = placeset).count()

    # augmentations (has task_id)
    context['links_added'] = PlaceLink.objects.filter(
      place_id_id__in = placeset, task_id__contains = '-').count()
    context['names_added'] = PlaceName.objects.filter(
      place_id_id__in = placeset, task_id__contains = '-').count()
    context['geoms_added'] = PlaceGeom.objects.filter(
      place_id_id__in = placeset, task_id__contains = '-').count()
    context['descriptions_added'] = PlaceDescription.objects.filter(
      place_id_id__in = placeset, task_id__contains = '-').count()

    print('context from DatasetDetailView',context)

    return context

# 
# confirm ok on delete
class DatasetDeleteView(DeleteView):
  template_name = 'datasets/dataset_delete.html'

  def get_object(self):
    id_ = self.kwargs.get("id")
    return get_object_or_404(Dataset, id=id_)

  def get_success_url(self):
    return reverse('dashboard')

# fetch places in specified dataset 
def ds_list(request, label):
  print('in ds_list() for',label)
  qs = Place.objects.all().filter(dataset=label)
  geoms=[]
  for p in qs.all():
    feat={"type":"Feature",
          "properties":{"src_id":p.src_id,"name":p.title},
              "geometry":p.geoms.first().jsonb}
    geoms.append(feat)
  return JsonResponse(geoms,safe=False)

def match_undo(request, ds, tid, pid):
  print('in match_undo() ds, task, pid:',ds,tid,pid)
  #ds=1;tid='d6ad4289-cae6-476d-873c-a81fed4d6315';pid=81474
  # 81474, 81445 (2), 81417, 81420, 81436, 81442, 81469
  geom_matches = PlaceGeom.objects.all().filter(task_id=tid, place_id_id=pid)
  link_matches = PlaceLink.objects.all().filter(task_id=tid, place_id_id=pid)
  geom_matches.delete()
  link_matches.delete()
  # match task_id, place_id_id in hits; set reviewed = false
  Hit.objects.filter(task_id=tid, place_id_id=pid).update(reviewed=False)
  return redirect('/datasets/'+str(ds)+'/review/'+tid+'/pass1')
 # /datasets/1/review/d6ad4289-cae6-476d-873c-a81fed4d6315/pass1
 
 
