from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import eventlet
import xmlrpc2.client

from sqlalchemy.orm.exc import NoResultFound

from warehouse import db, script
from warehouse.packages.models import Project, Version


eventlet.monkey_patch()


def filter_dict(d, required=None):
    if required is None:
        required = set()

    data = {}
    for key, value in d.items():
        if value is None:
            continue
        elif not key in required and value in ["None", "UNKNOWN"]:
            continue
        elif isinstance(value, (basestring, list, tuple, set)) and not value:
            continue
        else:
            data[key] = value
    return data


class PyPIFetcher(object):

    def __init__(self):
        # TODO(dstufft): Switch this to using verified SSL
        self.client = xmlrpc2.client.Client("http://pypi.python.org/pypi")

    def version(self, project, version):
        """
        Takes a project and version and it returns the normalized data for the
        release of project with that version.
        """
        data = self.client.release_data(project, version)
        data = filter_dict(data, required=set(["name", "version"]))

        # TODO(dstufft): Validate incoming data

        # fix classifiers (dedupe + sort)
        data["classifiers"] = list(set(data.get("classifiers", [])))
        data["classifiers"].sort()

        # Filter resulting dictionary down to only the required keys
        keys = {
            "name", "version", "summary", "description", "author",
            "author_email", "maintainer", "maintainer_email", "license",
            "requires_python", "requires_external", "bugtrack_url",
            "home_page", "project_url", "keywords", "download_url",
        }
        data = {key: value for key, value in data.items() if key in keys}

        return data

    def project(self, project):
        """
        Takes a project and returns all the normalized data for all the
        versions of that project.
        """
        versions = self.client.package_releases(project, True)

        # TODO(dstufft): Validate incoming data

        return [self.version(project, v) for v in versions]

    def packages(self):
        """
        Returns a list of all project names
        """
        return self.client.list_packages()


def store_project(name):
    try:
        project = Project.query.filter_by(name=name).one()
    except NoResultFound:
        project = Project(name)
        db.session.add(project)
        db.session.flush()

    return project


def store(release):
    project = store_project(release["name"])

    try:
        version = Version.query.filter_by(project=project,
                                          version=release["version"]).one()
    except NoResultFound:
        version = Version(project=project, version=release["version"])
        db.session.add(version)
        db.session.flush()

    version.summary = release.get("summary", "")
    version.description = release.get("description", "")

    version.author = release.get("author", "")
    version.author_email = release.get("author_email", "")

    version.maintainer = release.get("maintainer", "")
    version.maintainer_email = release.get("maintainer_email", "")

    version.license = release.get("maintainer", "")

    version.requires_python = release.get("requires_python", "")
    version.requires_external = release.get("requires_external", [])

    split_key = "," if "," in release.get("keywords", "") else None

    version.keywords = [x.strip()
                        for x in release.get("keywords", "").split(split_key)
                        if x.strip()]

    # Handle URIS
    uris = {}

    if release.get("bugtrack_url"):
        uris["Bugtracker"] = release["bugtrack_url"]

    if release.get("home_page"):
        uris["Home page"] = release["home_page"]

    if release.get("project_url"):
        for purl in release["project_url"]:
            label, url = [x.strip() for x in purl.split(",")]
            uris[label] = url

    version.uris = uris
    version.download_uri = release.get("download_url", "")

    # TODO(dstufft): Remove no longer existing Files

    return project, version


def syncer(project=None, version=None, fetcher=None):
    if project is None and not version is None:
        raise TypeError("Cannot have a version without a project")

    if fetcher is None:
        fetcher = PyPIFetcher()

    projects = [project] if not project is None else fetcher.packages()

    if version is None:
        pool = eventlet.GreenPool(100)

        for releases in pool.imap(fetcher.project, projects):
            # Take the releases and add them to the database
            for release in releases:
                store(release)

            # TODO(dstufft): Remove no longer existing releases

            # Commit the session to the Database
            db.session.commit()

        # Make sure that all projects exist in the database
        for project in projects:
            store_project(project)
    else:
        # Fetch and store the release
        store(fetcher.version(project, version))

    # TODO(dstufft): Remove no longer existing projects

    # Commit the session to the Database
    db.session.commit()


@script.command
def synchronize():
    syncer()
