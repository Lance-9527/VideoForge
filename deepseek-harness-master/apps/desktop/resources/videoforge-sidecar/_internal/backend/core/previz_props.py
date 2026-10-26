"""Deterministic prop anchors and explicit support relations, shared by previz compilers."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

DEFAULT_DIMENSIONS={
    'table':(1.2,.7,.75),'desk':(1.2,.7,.75),'box':(.8,.8,.6),'cube':(.6,.6,.6),
    'bed':(1.9,1.,.45),'sofa':(1.8,.85,.42),
}


def normalize_props(raw: list[dict[str,Any]],text: str='') -> list[dict[str,Any]]:
    """z always denotes the base. on_surface names an ID, never a guessed world height.

    Legacy z-only records retain their base semantics. The only inferred relation is an
    unambiguous single box/product explicitly described on a single table; richer scenes
    must supply IDs and on_surface instead of relying on proximity.
    """
    props=copy.deepcopy(raw)
    used=set()
    boxes=[p for p in props if p.get('type') in ('box','cube')]
    cube_intent=len(boxes)==1 and bool(re.search(r'立方体|正方体|立方|方块',text))
    for index,p in enumerate(props,1):
        identifier=str(p.get('id') or f'prop_{index}')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',identifier) or identifier in used:
            raise ValueError('道具ID须唯一，仅能包含字母、数字、下划线和短横线')
        p['id']=identifier;used.add(identifier)
        kind=str(p.get('type') or 'box').lower();p['type']=kind
        defaults=DEFAULT_DIMENSIONS.get(kind)
        if defaults:
            values=[float(p.get(key) or 0) for key in ('w','d','h')]
            if kind=='cube' or (kind=='box' and cube_intent):
                edge=next((value for value in values if value>0),.6)
                defaults=(edge,edge,edge)
            for key,default in zip(('w','d','h'),defaults):p[key]=float(p.get(key) or default)
        anchor=str(p.get('anchor') or 'base').lower()
        if anchor not in ('base','center'):raise ValueError('道具anchor只支持base或center')
        if anchor=='center':
            if not defaults:raise ValueError('该道具不支持center锚点，请提供base高度')
            p['z']=float(p.get('z') or 0)-float(p['h'])/2
        p['anchor']='base'
        for field in ('x','y','z','w','d','h'):
            if not math.isfinite(float(p.get(field) or 0)):raise ValueError('道具尺寸/位置必须是有限数字')
    tables=[p for p in props if p.get('type') in ('table','desk')]
    if len(boxes)==1 and len(tables)==1 and not boxes[0].get('on_surface'):
        # No match may cross a clause boundary or turn "next to the table" into contact.
        relation=re.search(r'(?:立方体|正方体|立方|方块|盒子?|箱子?|产品|商品)[^，,。；;\n]{0,18}'
                           r'(?:摆|放|置|立|搁|在)[^，,。；;\n]{0,24}'
                           r'(?:桌面上|桌上|桌面(?!旁|外)|展台上|展示台上|台座上|台面上)',text)
        if relation and not re.search(r'不|禁止|避免|不要|无需',relation.group()):
            boxes[0]['on_surface']=tables[0]['id'];boxes[0]['support_source']='explicit_text_relation'
    by_id={p['id']:p for p in props}
    resolved=set();active=set()
    def resolve(prop):
        identifier=prop['id']
        if identifier in resolved:return
        if identifier in active:raise ValueError('道具支撑关系形成循环')
        active.add(identifier)
        target=prop.get('on_surface')
        if target:
            support=by_id.get(str(target))
            if support is None:
                matches=[p for p in props if p['type']==target]
                support=matches[0] if len(matches)==1 else None
            if support is None:raise ValueError('on_surface必须指向已存在且唯一的道具ID')
            if support['type'] not in ('table','desk','box','cube'):raise ValueError('该支撑类型没有规范平面，请提供显式base高度')
            resolve(support)
            prop['on_surface']=support['id']
            prop['z']=float(support.get('z') or 0)+float(support['h'])
        active.remove(identifier);resolved.add(identifier)
    for prop in props:resolve(prop)
    return props
