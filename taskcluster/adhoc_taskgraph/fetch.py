# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Support for running tasks that download remote content and re-export
# it as task artifacts.

from __future__ import absolute_import, unicode_literals

import os
import re

from copy import deepcopy
from voluptuous import (
    Any,
    Optional,
    Required,
)

import taskgraph

from taskgraph.transforms.base import (
    TransformSequence,
)
from taskgraph.util.cached_tasks import (
    add_optimization,
)
from taskgraph.util.schema import (
    Schema,
)
from adhoc_taskgraph.signing_manifest import get_manifest


CACHE_TYPE = 'content.v1'

FETCH_SCHEMA = Schema({
    # Name of the task.
    Required('name'): basestring,

    # Relative path (from config.path) to the file the task was defined
    # in.
    Optional('job-from'): basestring,

    # Description of the task.
    Required('description'): basestring,

    Required('fetch'): Any(
        {
            'type': 'static-url',

            # The URL to download.
            Required('url'): basestring,

            # The SHA-256 of the downloaded content.
            Required('sha256'): basestring,

            # Size of the downloaded entity, in bytes.
            Required('size'): int,

            # GPG signature verification.
            Optional('gpg-signature'): {
                # URL where GPG signature document can be obtained. Can contain the
                # value ``{url}``, which will be substituted with the value from
                # ``url``.
                Required('sig-url'): basestring,
                # Path to file containing GPG public key(s) used to validate
                # download.
                Required('key-path'): basestring,
            },

            # The name to give to the generated artifact. Defaults to the file
            # portion of the URL. Using a different extension converts the
            # archive to the given type. Only conversion to .tar.zst is
            # supported.
            Optional('artifact-name'): basestring,

            # Strip the given number of path components at the beginning of
            # each file entry in the archive.
            # Requires an artifact-name ending with .tar.zst.
            Optional('strip-components'): int,

            # Add the given prefix to each file entry in the archive.
            # Requires an artifact-name ending with .tar.zst.
            Optional('add-prefix'): basestring,

            # IMPORTANT: when adding anything that changes the behavior of the task,
            # it is important to update the digest data used to compute cache hits.
        },
    ),
})

transforms = TransformSequence()


@transforms.add
def from_manifests(config, jobs):
    manifests = get_manifest()
    for job in jobs:
        for manifest in manifests:
            task = deepcopy(job)
            fetch = task.setdefault("fetch", {})
            fetch["url"] = manifest["url"]
            fetch["sha256"] = manifest["sha256"]
            fetch["size"] = manifest["filesize"]
            for k in ("gpg-signature", "artifact-name"):
                if manifest.get(k):
                    fetch[k] = manifest[k]
            yield task


transforms.add_validate(FETCH_SCHEMA)


@transforms.add
def process_fetch_job(config, jobs):
    # Converts fetch-url entries to the job schema.
    for job in jobs:
        yield create_fetch_url_task(config, job)


def make_base_task(config, name, description, command):
    # Fetch tasks are idempotent and immutable. Have them live for
    # essentially forever.
    if config.params['level'] == '3':
        expires = '1000 years'
    else:
        expires = '28 days'

    return {
        'attributes': {},
        'name': name,
        'description': description,
        'expires-after': expires,
        'label': 'fetch-%s' % name,
        'run-on-projects': [],
        'run': {
            'using': 'run-task',
            'checkout': False,
            'command': command,
        },
        'worker-type': 'images',
        'worker': {
            'chain-of-trust': True,
            'docker-image': {'in-tree': 'fetch'},
            'env': {},
            'max-run-time': 900,
        },
    }


def create_fetch_url_task(config, job):
    name = job['name']
    fetch = job['fetch']

    artifact_name = fetch.get('artifact-name')
    if not artifact_name:
        artifact_name = fetch['url'].split('/')[-1]

    command = [
        '/builds/worker/bin/fetch-content', 'static-url',
    ]

    # Arguments that matter to the cache digest
    args = [
        '--sha256', fetch['sha256'],
        '--size', '%d' % fetch['size'],
    ]

    if fetch.get('strip-components'):
        args.extend(['--strip-components', '%d' % fetch['strip-components']])

    if fetch.get('add-prefix'):
        args.extend(['--add-prefix', fetch['add-prefix']])

    command.extend(args)

    env = {}

    if 'gpg-signature' in fetch:
        sig_url = fetch['gpg-signature']['sig-url'].format(url=fetch['url'])
        key_path = os.path.join(taskgraph.GECKO, fetch['gpg-signature'][
            'key-path'])

        with open(key_path, 'rb') as fh:
            gpg_key = fh.read()

        env['FETCH_GPG_KEY'] = gpg_key
        command.extend([
            '--gpg-sig-url', sig_url,
            '--gpg-key-env', 'FETCH_GPG_KEY',
        ])

    command.extend([
        fetch['url'], '/builds/worker/artifacts/%s' % artifact_name,
    ])

    task = make_base_task(config, name, job['description'], command)
    task['worker']['artifacts'] = [{
        'type': 'directory',
        'name': 'adhoc',
        'path': '/builds/worker/artifacts',
    }]
    task['worker']['env'] = env
    task['attributes']['fetch-artifact'] = 'releng/adhoc/%s' % artifact_name

    if not taskgraph.fast:
        cache_name = task['label'].replace('{}-'.format(config.kind), '', 1)

        # This adds the level to the index path automatically.
        add_optimization(
            config,
            task,
            cache_type=CACHE_TYPE,
            cache_name=cache_name,
            # We don't include the GPG signature in the digest because it isn't
            # materially important for caching: GPG signatures are supplemental
            # trust checking beyond what the shasum already provides.
            digest_data=args + [artifact_name],
        )

    return task