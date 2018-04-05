#!/usr/bin/env python

import aiohttp
import argparse
import asyncio
import async_timeout
import posixpath
from taskcluster.async import Index
from taskcluster.async import Queue

from releasewarrior.wiki_data import get_current_build_index


HG_URL = 'https://hg.mozilla.org/'

REPOS = ('releases/mozilla-beta', 'releases/mozilla-release',
         'releases/mozilla-esr52', 'mozilla-central')

""" http://mozilla-version-control-tools.readthedocs.io/en/latest/hgmo/pushlog.html?highlight=json-pushes#json-pushes-command """  # noqa
JSON_PUSHES = 'json-pushes?changeset={rev}&version=2&tipsonly=1&full=1'

ACTION_INDEX = 'gecko.v2.{repo}.pushlog-id.{pushid}.actions'


async def get_changeset_data(revision, session):
    for repo in REPOS:
        url = posixpath.join(HG_URL, repo, JSON_PUSHES.format(rev=revision))
        async with async_timeout.timeout(10):
            async with session.get(url) as response:
                if response.status == 404:
                    continue
                response.raise_for_status()

                index_repo = repo[repo.rfind('/') + 1:]
                data = await response.json()
                pushlog_id = list(data['pushes'].keys())[0]
                changeset = data['pushes'][pushlog_id]['changesets'][-1]
                return (changeset, index_repo, pushlog_id)


async def get_action_tasks(session, pushlog_id, index_repo):
    async with async_timeout.timeout(100):
        index_string = ACTION_INDEX.format(pushid=pushlog_id, repo=index_repo)
        index = Index(session=session)
        data = await index.listTasks(index_string, dict(limit=100))
        tasks = [t['taskId'] for t in data['tasks']]
        return tasks


async def get_action_task_details(session, taskid):
    async with async_timeout.timeout(100):
        queue = Queue(session=session)
        task = await queue.task(taskid)
        return dict(taskid=taskid,
                    name=task['extra']['action']['name'],
                    buildnum=task['extra']['action']['context']['input']['build_number'],
                    flavor=task['extra']['action']['context']['input']['release_promotion_flavor'],
                    ci=task['taskGroupId'])


def filter_tasks(tasks, product, version, buildnum):
    product_tasks = [t for t in tasks if product in t['flavor']]
    if not product_tasks:
        print("Couldn't locate product tasks for {}".format(product))
    buildnumbers = set([t['buildnum'] for t in product_tasks])
    if buildnum not in buildnumbers:
        print("Couldn't locate build number {} for {} {}".format(buildnum, product, version))

    for ci in set([t['ci'] for t in product_tasks if t['buildnum'] == buildnum]):
        promote = [t['taskid'] for t in product_tasks
                   if t['buildnum'] == buildnum and t['ci'] == ci and
                   'promote_' in t['flavor']]
        push = [t['taskid'] for t in product_tasks
                if t['buildnum'] == buildnum and t['ci'] == ci and
                'push_' in t['flavor']]
        ship = [t['taskid'] for t in product_tasks
                if t['buildnum'] == buildnum and t['ci'] == ci and
                'ship_' in t['flavor']]
        if len(promote) > 1 or len(push) > 1 or len(ship) > 1:
            raise Exception("Found too many relevant graphs.")
        # This is the format from the .json store.
        results = [
            ['decision', ci]
        ]
        if promote:
            results.append(['promote', promote[0]])
        if push:
            results.append(['push', push[0]])
        if ship:
            results.append(['ship', ship[0]])
        return results


def output_graphs(groupids, product, version, revision, logger):
    if revision:
        revision = " (rev: {})".format(revision)
    else:
        revision = ""
    logger.info("Environment variables for {} {}{}".format(product, version, revision))
    print()
    for group in groupids:
        print("export {label}_TASK_ID={groupid}".format(label=group[0].upper(), groupid=group[1]))


async def query_action_tasks(loop, product, version, buildnum, revision):
    async with aiohttp.ClientSession(loop=loop) as session:
        cset_data = await get_changeset_data(revision, session)
        changeset, index_repo, pushlog_id = cset_data
        possible_tasks = await get_action_tasks(session, pushlog_id, index_repo)
        all_relevant_tasks = []
        for taskid in possible_tasks:
            task_data = await get_action_task_details(session, taskid)
            if task_data['name'] != 'release-promotion':
                continue
            all_relevant_tasks.append(task_data)
        return filter_tasks(all_relevant_tasks, product, version, buildnum)


def display_graphids(data, product, version, revision, logger):
    current_build_index = get_current_build_index(data)
    buildnum = data['inflight'][current_build_index]['buildnum']
    if not revision:
        revision = data['inflight'][current_build_index].get('revision')
    groupids = dict()
    if len(data['inflight'][current_build_index]['graphids']) > 0:
        groupids = data['inflight'][current_build_index]['graphids']
        output_graphs(groupids, product, version, revision, logger)
    elif revision:
        loop = asyncio.get_event_loop()
        groupids = loop.run_until_complete(query_action_tasks(
            loop, product, version, buildnum, revision))
        output_graphs(groupids, product, version, revision, logger)
    else:
        logger.error('No stored graphids or revision provided. Maybe check ship-it?')
