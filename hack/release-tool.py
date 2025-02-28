#!/usr/bin/python3

import argparse
import json
import logging
import time

import openshift as oc
from openshift import OpenShiftPythonException

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logger = logging.getLogger('releaseTool')

SUPPORTED_PRODUCTS = ['ocp', 'okd']
SUPPORTED_ARCHITECTURES = ['amd64', 'arm64', 'ppc64le', 's390x', 'multi']


def generate_resource_values(product, stream_name, architecture, private):
    arch_suffix, private_suffix = "", ""

    if product == 'okd':
        return 'origin', stream_name

    if architecture != 'amd64':
        arch_suffix = f'-{architecture}'

    if private:
        private_suffix = '-priv'

    namespace = f'{product}{arch_suffix}{private_suffix}'
    imagestream = f'{stream_name}{arch_suffix}{private_suffix}'

    return namespace, imagestream


def validate_server_connection(ctx):
    with oc.options(ctx), oc.tracking(), oc.timeout(60):
        try:
            username = oc.whoami()
            version = oc.get_server_version()
            logger.debug(f'Connected to APIServer running version: {version}, as: {username}')
        except (ValueError, OpenShiftPythonException, Exception) as e:
            logger.error(f"Unable to verify cluster connection using context: \"{ctx['context']}\"")
            raise e


def create_imagestreamtag_patch(action, custom_message, custom_reason):
    data = {
        'image': {
            'metadata': {
                'annotations': {}
            }
        },
        'metadata': {
            'annotations': {}
        },
        'tag': {
            'annotations': {}
        }
    }

    if action == 'accept':
        phase = 'Accepted'
    elif action == 'reject':
        phase = 'Rejected'
    else:
        raise ValueError(f'Unsupported action specified: {action}')

    message = f'Manually {action}ed per TRT'
    if custom_message is not None:
        message = custom_message

    annotations = {
        'phase': phase,
        'message': message
    }

    if custom_reason is not None:
        annotations['reason'] = custom_reason

    for key, value in annotations.items():
        if value is not None:
            annotation = 'release.openshift.io/' + key
            data['image']['metadata']['annotations'][annotation] = value
            data['metadata']['annotations'][annotation] = value
            data['tag']['annotations'][annotation] = value

    return data


def write_backup_file(name, release, data):
    ts = int(round(time.time() * 1000))
    backup_filename = f'{name}_{release}-{ts}.json'

    with open(backup_filename, mode='w+', encoding='utf-8') as backup:
        logger.debug(f'Creating backup file: {backup_filename}')
        backup.write(json.dumps(data, indent=4))

    return backup_filename


def patch_imagestreamtag(ctx, namespace, imagestream, action, release, custom_message, custom_reason, execute):
    patch = create_imagestreamtag_patch(action, custom_message, custom_reason)
    logger.debug(f'Generated oc patch:\n{json.dumps(patch, indent=4)}')

    with oc.options(ctx), oc.tracking(), oc.timeout(15):
        try:
            with oc.project(namespace):
                tag = oc.selector(f'imagestreamtag/{imagestream}:{release}').object(ignore_not_found=True)
                if not tag:
                    logger.error(f'Unable to locate imagestreamtag: {namespace}/{imagestream}:{release}')
                    return

                logger.info(f'{action.capitalize()}ing imagestreamtag: {namespace}/{imagestream}:{release}')
                if execute:
                    backup_file = write_backup_file(imagestream, release, tag.model._primitive())

                    tag.patch(patch)

                    logger.info(f'Release {release} updated successfully')
                    logger.info(f'Backup written to: {backup_file}')
                else:
                    logger.info(f'[dry-run] Patching release {release} with patch:\n{json.dumps(patch, indent=4)}')
                    logger.warning('You must specify "--execute" to permanently apply these changes')

        except (ValueError, OpenShiftPythonException, Exception) as e:
            logger.error(f'Unable to update release: "{release}"')
            raise e


def create_releasepayload_patch(action, custom_reason):
    if action == 'accept':
        override = 'Accepted'
    elif action == 'reject':
        override = 'Rejected'
    else:
        raise ValueError(f'Unsupported action specified: {action}')

    reason = f'Manually {action}ed per TRT'
    if custom_reason is not None:
        reason = custom_reason

    data = {
        'spec': {
            'payloadOverride': {
                'override': override,
                'reason': reason,
            }
        }
    }

    return data


def patch_releaespayload(ctx, namespace, action, release, custom_reason, execute):
    patch = create_releasepayload_patch(action, custom_reason)
    logger.debug(f'Generated oc patch:\n{json.dumps(patch, indent=4)}')

    with oc.options(ctx), oc.tracking(), oc.timeout(15):
        try:
            with oc.project(namespace):
                payload = oc.selector(f'releasepayload/{release}').object(ignore_not_found=True)
                if not payload:
                    logger.error(f'Unable to locate releasepayload: {namespace}/{release}')
                    return

                logger.info(f'{action.capitalize()}ing releasepayload: {namespace}/{release}')
                if execute:
                    backup_file = write_backup_file("releasepayload", release, payload.model._primitive())

                    payload.patch(patch, strategy='merge')

                    logger.info(f'ReleasePayload {release} updated successfully')
                    logger.info(f'Backup written to: {backup_file}')
                else:
                    logger.info(f'[dry-run] Patching releasepayload {release} with patch:\n{json.dumps(patch, indent=4)}')
                    logger.warning('You must specify "--execute" to permanently apply these changes')

        except (ValueError, OpenShiftPythonException, Exception) as e:
            logger.error(f'Unable to update releasepayload: "{release}"')
            raise e


def prune_releases(ctx, namespace, imagestream, releases, execute, confirm):
    for tag in releases:
        if execute:
            delete_imagestreamtag(ctx, namespace, imagestream, tag, confirm)
        else:
            logger.info(f'[dry-run] Deleting imagestreamtag: {namespace}/{imagestream}:{tag}')
            logger.warning('You must specify "--execute" to permanently apply these changes')


def delete_imagestreamtag(ctx, namespace, imagestream, tag, confirm):
    imagestreamtag = f'{imagestream}:{tag}'

    with oc.options(ctx), oc.tracking(), oc.timeout(15):
        try:
            with oc.project(namespace):
                result = oc.selector(f'imagestreamtag/{imagestreamtag}').object(ignore_not_found=True)

                if result is not None:
                    # Check for the release-controllers "keep" annotation...
                    keep = result.get_annotation("release.openshift.io/keep", if_missing=None)

                    if keep is None and (confirm or confirm_delete(namespace, imagestreamtag)):
                        logger.info(f'Deleting imagestreamtag: {namespace}/{imagestreamtag}')

                        backup_file = write_backup_file("imagestreamtag", tag, result.model._primitive())
                        logger.info(f'Backup written to: {backup_file}')

                        r = result.delete(ignore_not_found=True)
                        if r.status() != 0:
                            logger.error(f'Delete returned: {r.out()}')
                    else:
                        if keep is not None:
                            logger.warning(f'Imagestreamtag: "{namespace}/{imagestreamtag}" has been flagged as "Keep", skipped.')
                        else:
                            logger.info(f'Deletion of imagestreamtag: "{namespace}/{imagestreamtag}" skipped.')
                else:
                    logger.info(f'Imagestreamtag: "{namespace}/{imagestreamtag}" does not exist.')
        except (ValueError, OpenShiftPythonException, Exception) as e:
            logger.error(f'Unable to delete imagestreamtag: {e}')
            raise e


def confirm_delete(namespace, imagestreamtag):
    i = 1
    while i <= 5:
        answer = input(f"Delete: {namespace}/{imagestreamtag}? (yes or no) ")
        if any(answer.lower() == f for f in ["yes", 'y', 'ye']):
            return True
        elif any(answer.lower() == f for f in ['no', 'n', '0']):
            return False
        else:
            print('Please enter yes or no')
            i = i + 1
    return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Manually accept or reject release payloads')
    parser.add_argument('-m', '--message', help='Specifies a custom message to include with the update', default=None)
    parser.add_argument('-r', '--reason', help='Specifies a custom reason to include with the update', default=None)
    parser.add_argument('--execute', help='Specify to persist changes on the cluster', action='store_true')

    config_group = parser.add_argument_group('Configuration Options')
    config_group.add_argument('-v', '--verbose', help='Enable verbose output', action='store_true')

    ocp_group = parser.add_argument_group('Openshift Configuration Options')
    ocp_group.add_argument('-c', '--context', help='The OC context to use (default is "app.ci")', default='app.ci')
    ocp_group.add_argument('-k', '--kubeconfig', help='The kubeconfig to use (default is "~/.kube/config")', default='')
    ocp_group.add_argument('-n', '--name', help='The product prefix to use (default is "ocp")', choices=SUPPORTED_PRODUCTS, default='ocp')
    ocp_group.add_argument('-i', '--imagestream', help='The name of the release imagestream to use (default is "release")', default='release')
    ocp_group.add_argument('-a', '--architecture', help='The architecture of the release to process (default is "amd64")', choices=SUPPORTED_ARCHITECTURES, default='amd64')
    ocp_group.add_argument('-p', '--private', help='Enable updates of "private" releases', action='store_true')

    subparsers = parser.add_subparsers(title='subcommands', description='valid subcommands', help='Supported operations', required=True)
    accept_parser = subparsers.add_parser('accept', help='Accepts the specified release')
    accept_parser.set_defaults(action='accept')
    accept_parser.add_argument('release', help='The name of the release to accept (i.e. 4.10.0-0.ci-2021-12-17-144800)')

    reject_parser = subparsers.add_parser('reject', help='Rejects the specified release')
    reject_parser.set_defaults(action='reject')
    reject_parser.add_argument('release', help='The name of the release to reject (i.e. 4.10.0-0.ci-2021-12-17-144800)')

    prune_parser = subparsers.add_parser('prune', help='Prunes the specified release(s)')
    prune_parser.set_defaults(action='prune')
    prune_parser.add_argument('releases', help='The name of the release(s) to prune (i.e. 4.10.0-0.ci-2021-12-17-144800)', action="extend", nargs="+", type=str)
    prune_parser.add_argument('-y', '--yes', help='Automatically answer yes to confirm deletion(s)', action='store_true')

    args = vars(parser.parse_args())

    if args['verbose']:
        logger.setLevel(logging.DEBUG)

    context = {"context": args['context']}

    if len(args['kubeconfig']) > 0:
        context['kubeconfig'] = args['kubeconfig']

    validate_server_connection(context)
    release_namespace, release_image_stream = generate_resource_values(args['name'], args['imagestream'], args['architecture'], args['private'])

    if args['action'] in ['accept', 'reject']:
        # TODO: Remove once ReleasePayloads are fully implemented...
        patch_imagestreamtag(context, release_namespace, release_image_stream, args['action'], args['release'], args['message'], args['reason'], args['execute'])

        patch_releaespayload(context, release_namespace, args['action'], args['release'], args['reason'], args['execute'])
    elif args['action'] == 'prune':
        prune_releases(context, release_namespace, release_image_stream, args['releases'], args['execute'], args['yes'])
    else:
        logger.error(f'Unsupported action: {args["action"]}')
        exit(-1)
