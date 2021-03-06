""" Finish Line!  A CLI tool for wrapping up your sprints.

See the README for more information.

Author:  Ralph Bean <rbean@redhat.com>

"""
from __future__ import print_function

import argparse
import collections
import datetime
import sys

import bs4
import docutils.examples
import jinja2

import jira

date_format = '%Y-%m-%d'

custom_filters = {
    'slugify': lambda x: x.lower().replace(' ', '-'),
    'rst2html': lambda rst: docutils.examples.html_parts(
        rst, input_encoding='utf-8')['body'],
    'replace': lambda string, char: char * len(string),
}


def scrape_links(session, args):
    response = session.get(args.server)
    soup = bs4.BeautifulSoup(response.text, 'html5lib')
    return [l.text for l in soup.find(id='content').findAll('a')[5:]]


def parse_arguments():
    two_weeks = datetime.timedelta(days=14)
    default_since = (datetime.date.today() - two_weeks).strftime(date_format)
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', help='server to your JIRA instance.')
    parser.add_argument('--cacert', help='CA cert for https validation.',
                        default='/etc/pki/tls/certs/ca-bundle.crt')
    parser.add_argument('--project', help='JIRA project to report on.')
    parser.add_argument('--since', help='Past date from which to pull data.',
                        default=default_since)
    parser.add_argument('--title', help='Title of the report.')
    parser.add_argument('--subtitle', help='Subtitle of the report.')
    parser.add_argument('--template', help='Path to a template for output.')
    parser.add_argument('--references', help='Path to an extra template.')
    parser.add_argument('--epic-field', help='Epic field key.',
                        default='customfield_10006')
    parser.add_argument('--hide-epics', help='Comma separated list of epics',
                        default=None)
    parser.add_argument('--include-epics', help='Comma separated list of epics',
                        default=None)
    parser.add_argument('--mvp-status-field', help='MVP status field key.',
                        default='customfield_11908')
    parser.add_argument('--story-point-field', help='Story point field key.',
                        default='customfield_10002')
    parser.add_argument('--default-story-points',
                        help='Default points to assume if an issue has none.',
                        type=float, default=3)
    parser.add_argument('--placeholder-objective',
                        help="Placeholder text for epics that don't "
                        "have an objective associated.",
                        default='Miscellaneous')
    parser.add_argument('--attribution', help='Show attribution for status.',
                        default=False, action='store_true')
    args = parser.parse_args()
    if not args.server:
        raise ValueError('--server is required')
    if not args.project:
        raise ValueError('--project is required')
    if not args.title:
        raise ValueError('--title is required')
    if not args.template:
        raise ValueError('--template is required')
    if args.hide_epics:
        args.hide_epics = args.hide_epics.split(',')
    else:
        args.hide_epics = []
    if args.include_epics:
        args.include_epics = args.include_epics.split(',')
    else:
        args.include_epics = []
    for epic in args.include_epics:
        if epic in args.hide_epics:
            raise ValueError("%r may not be both hidden and included." % epic)
    return args


def render(args, data):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader('templates'),
    )
    env.filters.update(custom_filters)
    template = env.get_template(args.template)

    data = data.copy()

    data['today'] = datetime.datetime.utcnow().strftime(date_format)

    data.update(args._get_kwargs())

    if args.references:
        references = env.get_template(args.references)
        data['references'] = references.render(**data)

    return template.render(**data)


def pull_issues(client, args):
    tmpl = '\n'.join([
        ' project = %s',
        ' AND (',
        '   resolution is EMPTY OR ',
        '   (',
        '       resolution is not EMPTY AND ',
        '       resolutiondate >= %s',
        '   )',
        ')',
        ' AND status != Dropped',
        ' AND statusCategory != "To Do"',
    ])
    query = tmpl % (args.project, args.since)
    print(query, file=sys.stderr)
    issues = client.search_issues(query, maxResults=999)
    for issue in issues:
        yield issue


def extract_status_update(args, epic):
    sentinnels = [
        'h1. Status Update:',
        'Status Update:',
        'h1. Status Update',
        'Status Update',
    ]
    for comment in reversed(epic.fields.comment.comments):
        for sentinnel in sentinnels:
            body = comment.body
            if body.startswith(sentinnel):
                # Attach a cleaned version for the template.
                body = body[len(sentinnel):].strip()
                body = body.split('\n')[0].strip()
                comment.cleaned = body
                return comment


def extract_objective(args, epic):
    for link in epic.raw['fields'].get('issuelinks', []):
        if link['type']['inward'] == 'is subtask of':
            return link['inwardIssue']['fields']['summary']
    return args.placeholder_objective


def extract_mvp_status(args, epic):
    return epic.raw['fields'].get(args.mvp_status_field)


def extract_target_date(args, epic):
    return epic.raw['fields']['duedate'] or ''


def extract_owner(args, epic):
    return epic.raw['fields']['assignee']


def extract_percent_complete(client, args, epic):
    tmpl = (
        'project = %s'
        ' AND "Epic Link" = %s'
    )
    query = tmpl % (args.project, epic.key)
    issues = client.search_issues(query)

    total_points = sum([
        i.raw['fields'][args.story_point_field] or args.default_story_points
        for i in issues
    ])
    closed_points = sum([
        i.raw['fields'][args.story_point_field] or args.default_story_points
        for i in issues
        if i.raw['fields']['resolution']
    ])
    if total_points:
        return "%0.1f" % (closed_points / total_points * 100)
    else:
        return "nan"  # not a number


def get_epic_details(client, args, key):
    if not key:
        return None
    epic = client.issue(key)

    # epic.image_url = 'https://placekitten.com/1600/900'
    epic.percent_complete = extract_percent_complete(client, args, epic)
    epic.status_update = extract_status_update(args, epic)
    epic.mvp_status = extract_mvp_status(args, epic)
    epic.target_date = extract_target_date(args, epic)
    epic.owner = extract_owner(args, epic)
    epic.objective = extract_objective(args, epic)

    return epic


def collate_issues(client, args, issues):
    epics = {}
    objectives = collections.defaultdict(set)
    by_epic = collections.defaultdict(lambda: collections.defaultdict(set))

    # First, handle any explicitly listed epics, even if they have no curent
    # work in the `issues` list.
    for epic_key in args.include_epics:
        if epic_key not in epics:
            epics[epic_key] = get_epic_details(client, args, epic_key)

        placeholder = args.placeholder_objective
        objective = getattr(epics[epic_key], 'objective', placeholder)
        objectives[objective].add(epic_key)

    # Second, do our main work of processing the individual issues passed in.
    for issue in issues:
        epic_key = issue.raw['fields'][args.epic_field]

        # Ignore orphan tasks, unassociated with an epic.
        if not epic_key:
            continue

        # If this is on a verboten epic, ditch it.
        if epic_key in args.hide_epics:
            continue

        # Enrich with details
        if epic_key not in epics:
            epics[epic_key] = get_epic_details(client, args, epic_key)

        placeholder = args.placeholder_objective
        objective = getattr(epics[epic_key], 'objective', placeholder)
        objectives[objective].add(epic_key)

        # Associate the issue with the enriched epic.
        print("Found %r on %r, under %r" % (issue.key, epic_key, objective),
              file=sys.stderr)
        category = issue.fields.status.raw['statusCategory']['name']
        by_epic[epic_key][category].add(issue)

    # Bubble up the KR completion to an average value for the objective.
    objective_completion = {}
    for objective in objectives:
        objective_completion[objective] = sum(
            float(epic.percent_complete)
            for key, epic in epics.items()
            if key in objectives[objective]
        ) / len(objectives[objective])

    return dict(
        epics=epics,
        by_epic=by_epic,
        objectives=objectives,
        objective_completion=objective_completion,
    )


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)

    args = parse_arguments()

    client = jira.client.JIRA(
        server=args.server,
        options=dict(verify=args.cacert),
        kerberos=True,
    )

    issues = pull_issues(client, args)
    data = collate_issues(client, args, issues)

    output = render(args, data)
    print(output.encode('utf-8'))
