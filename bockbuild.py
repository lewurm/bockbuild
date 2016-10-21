#!/usr/bin/python -u -OO

import os
from optparse import OptionParser
from bockbuild.util.util import *
from bockbuild.util.csproj import *
from bockbuild.environment import Environment
from bockbuild.package import *
from bockbuild.profile import Profile
import collections
import hashlib
import itertools
import traceback
from collections import namedtuple

ProfileDesc = namedtuple ('Profile', 'name description path modes')
active_profile = None
bockbuild = None

def find_profiles (base_path):
    assert Profile.loaded == None
    sys.path.append('%s/bockbuild' % base_path)
    profiles = []
    resolved_names = []
    while True:
        progress_made = False
        for path in iterate_dir ('%s/bockbuild' % base_path, with_dirs=True):
            file = '%s/profile.py' % path
            if os.path.isdir (path) and os.path.isfile (file):
                name = os.path.basename (path)
                if name in resolved_names:
                    continue

                fail = None
                profile = None
                try:
                    execfile(file, globals())
                    if not Profile.loaded:
                        fail = 'No profile loaded'
                    profile = Profile.loaded
                except Exception as e:
                    fail = e
                finally:
                    Profile.loaded = None

                if not fail:
                    profile = Profile.loaded
                    Profile.loaded = None
                    progress_made = True
                    description = ""
                    if hasattr(profile, 'description'):
                        description = profile.__class__.description
                    profiles.append (ProfileDesc (name = name, description = description, path = path, modes = ""))
                    resolved_names.append(name)
                else:
                    warn(fail)

        if not progress_made:
            break
    assert Profile.loaded == None
    return profiles

class Bockbuild:

    def run(self):
        self.name = 'bockbuild'
        self.root = os.path.dirname (os.path.realpath(__file__)) # Bockbuild system root
        config.protected_git_repos.append (self.root)
        self.execution_root = os.getcwd()
        self.resources = set([os.path.realpath(
            os.path.join(self.root, 'packages'))]) # list of paths on where to look for packages, patches, etc.
        self.build_root = os.path.join(self.root, 'builds')
        self.staged_prefix = os.path.join(self.root, 'stage')
        self.toolchain_root = os.path.join(self.root, 'toolchain')
        self.artifact_root = os.path.join(self.root, 'artifacts')
        self.package_root = os.path.join(self.root, 'distribution')
        self.scratch = os.path.join(self.root, 'scratch')
        self.logs = os.path.join(self.root, 'logs')
        self.env_file = os.path.join(self.root, 'last-successful-build.env')
        self.source_cache = os.getenv('BOCKBUILD_SOURCE_CACHE') or os.path.realpath(
            os.path.join(self.root, 'cache'))
        self.cpu_count = get_cpu_count()
        self.host = get_host()
        self.uname = backtick('uname -a')

        self.env = Environment(self)
        self.env.set('BUILD_ARCH', '%{arch}')

        self.full_rebuild = False

        self.toolchain = []

        find_git(self)
        self.bockbuild_rev = git_get_revision(self, self.root)
        self.profile_root = git_rootdir (self, self.execution_root)
        self.profiles = find_profiles (self.profile_root)

        for profile in self.profiles:
            self.resources.add(profile.path)

        loginit('bockbuild (%s)' % (git_shortid(self, self.root)))
        info('cmd: %s' % ' '.join(sys.argv))

        if len (sys.argv) < 2:
            info ('Profiles in %s --' % self.git ('config --get remote.origin.url', self.profile_root)[0])
            info(map (lambda x: '\t%s: %s' % (x.name, x.description), self.profiles))
            finish()

        global active_profile
        Package.profile = active_profile = self.load_profile (sys.argv[1])

        self.parser = self.init_parser()
        self.cmd_options, self.cmd_args = self.parser.parse_args(sys.argv[2:])

        self.packages_to_build = self.cmd_args or active_profile.packages


        active_profile.setup()
        self.verbose = self.cmd_options.verbose
        config.verbose = self.cmd_options.verbose
        self.arch = self.cmd_options.arch
        self.unsafe = self.cmd_options.unsafe
        config.trace = self.cmd_options.trace
        self.tracked_env = []



        ensure_dir(self.source_cache, purge=False)
        ensure_dir(self.artifact_root, purge=False)
        ensure_dir(self.build_root, purge=False)
        ensure_dir(self.scratch, purge=True)
        ensure_dir(self.logs, purge=False)

        self.build()

    def init_parser(self):
        parser = OptionParser(
            usage='usage: %prog [options] [package_names...]')
        parser.add_option('--build',
                          action='store_true', dest='do_build', default=True,
                          help='build the profile')
        parser.add_option('--package',
                          action='store_true', dest='do_package', default=False,
                          help='package the profile')
        parser.add_option('--verbose',
                          action='store_true', dest='verbose', default=False,
                          help='show all build output (e.g. configure, make)')
        parser.add_option('-d', '--debug', default=False,
                          action='store_true', dest='debug',
                          help='Build with debug flags enabled')
        parser.add_option('-e', '--environment', default=False,
                          action='store_true', dest='dump_environment',
                          help='Dump the profile environment as a shell-sourceable list of exports ')
        parser.add_option('-r', '--release', default=False,
                          action='store_true', dest='release_build',
                          help='Whether or not this build is a release build')
        parser.add_option('', '--csproj-env', default=False,
                          action='store_true', dest='dump_environment_csproj',
                          help='Dump the profile environment xml formarted for use in .csproj files')
        parser.add_option('', '--csproj-insert', default=None,
                          action='store', dest='csproj_file',
                          help='Inserts the profile environment variables into VS/MonoDevelop .csproj files')
        parser.add_option('', '--arch', default='default',
                          action='store', dest='arch',
                          help='Select the target architecture(s) for the package')
        parser.add_option('', '--shell', default=False,
                          action='store_true', dest='shell',
                          help='Get an shell with the package environment')
        parser.add_option('', '--unsafe', default=False,
                          action='store_true', dest='unsafe',
                          help='Prevents full rebuilds when a build environment change is detected. Useful for debugging.')
        parser.add_option('', '--trace', default=False,
                          action='store_true', dest='trace',
                          help='Enable tracing (for diagnosing bockbuild problems')

        return parser

    def build_distribution(self, packages, dest, stage, arch):
        # TODO: full relocation means that we shouldn't need dest at this stage
        build_list = []
        stage_invalidated = False #if anything is dirty we flush the stageination path and fill it again

        progress('Fetching packages')
        for package in packages.values():
            package.build_artifact = os.path.join(
                self.artifact_root, '%s-%s' % (package.name, arch))
            package.buildstring_file = package.build_artifact + '.buildstring'
            package.log = os.path.join(self.logs, package.name + '.log')
            if os.path.exists(package.log):
                delete(package.log)

            retry(package.fetch)

            if self.full_rebuild:
                package.request_build('Full rebuild')

            elif not os.path.exists(package.build_artifact):
                package.request_build('No artifact')

            elif is_changed(package.buildstring, package.buildstring_file):
                package.request_build('Updated')

            if package.needs_build:
                build_list.append(package)
                stage_invalidated = True

        verbose('%d packages need building:' % len(build_list))
        verbose(['%s (%s)' % (x.name, x.needs_build) for x in build_list])

        if stage_invalidated:
            ensure_dir (stage, purge = True)

        for package in packages.values():
            package.start_build(arch, stage, stage)
            # make artifact in scratch
            # delete artifact + buildstring
            with open(package.buildstring_file, 'w') as output:
                output.write('\n'.join(package.buildstring))

    def build(self):
        profile = active_profile

        if self.cmd_options.dump_environment:
            self.env.compile()
            self.env.dump()
            sys.exit(0)

        if self.cmd_options.dump_environment_csproj:
            # specify to use our GAC, else MonoDevelop would
            # use its own
            self.env.set('MONO_GAC_PREFIX', self.staged_prefix)

            self.env.compile()
            self.env.dump_csproj()
            sys.exit(0)

        if self.cmd_options.csproj_file is not None:
            self.env.set('MONO_GAC_PREFIX', self.staged_prefix)
            self.env.compile()
            self.env.write_csproj(self.cmd_options.csproj_file)
            sys.exit(0)

        profile.toolchain_packages = collections.OrderedDict()
        for source in self.toolchain:
            package = self.load_package(source)
            profile.toolchain_packages[package.name] = package

        profile.release_packages = collections.OrderedDict()
        for source in self.packages_to_build:
            package = self.load_package(source)
            profile.release_packages[package.name] = package

        profile.setup_release()

        if self.track_env():
            if self.unsafe:
                warn('Build environment changed, but overriding full rebuild!')
            else:
                info('Build environment changed, full rebuild triggered')
                self.full_rebuild = True
                ensure_dir(self.build_root, purge=True)

        if self.cmd_options.shell:
            title('Shell')
            self.shell()

        if self.cmd_options.do_build:
            title('Building toolchain')
            self.build_distribution(
                profile.toolchain_packages, self.toolchain_root, self.toolchain_root, arch='toolchain')

            title('Building release')
            self.build_distribution(
                profile.release_packages, profile.prefix, self.staged_prefix, arch=self.arch)

            # update env
            with open(self.env_file, 'w') as output:
                output.write('\n'.join(self.tracked_env))

        if self.cmd_options.do_package:
            title('Packaging')
            protect_dir(self.staged_prefix)
            ensure_dir(self.package_root, True)

            run_shell('rsync -aPq %s/* %s' %
                      (self.staged_prefix, self.package_root), False)
            unprotect_dir(self.package_root)

            self.process_release(self.package_root)
            self.package()

    def track_env(self):
        self.env.compile()
        self.env.export()
        self.env_script = os.path.join(
            self.root, self.profile_name) + '_env.sh'
        self.env.write_source_script(self.env_script)

        if not os.path.exists (self.env_file):
            return False
        self.tracked_env.extend(self.env.serialize())
        return is_changed(self.tracked_env, self.env_file)

    def load_package(self, source):
        if isinstance(source, Package):  # package can already be loaded in the source list
            return source

        fullpath = None
        for i in self.resources:
            candidate_fullpath = os.path.join(i, source + '.py')
            trace (candidate_fullpath)
            if os.path.exists(candidate_fullpath):
                if fullpath is not None:
                    error ('Package "%s" resolved in multiple locations (search paths: %s' % (source, self.resources))
                fullpath = candidate_fullpath
            
        if not fullpath:
            error("Package '%s' not found ('search paths: %s')" % (source, self.resources))

        Package.last_instance = None

        execfile(fullpath, globals())

        if Package.last_instance is None:
            error('%s does not provide a valid package.' % source)

        new_package = Package.last_instance
        new_package._path = fullpath
        return new_package

    def load_profile(self, source):
        if Profile.loaded:
            error ('A profile is already loaded: %s' % Profile.loaded)
        path = None
        for profile in self.profiles:
            if profile.name == source:
                path = profile.path

        if path == None:
            if isinstance(source, Profile):  # package can already be loaded in the source list
                Profile.loaded = source
            else:
                error("Profile '%s' not found" % source)

        fullpath = os.path.join(path, 'profile.py')

        if not os.path.exists(fullpath):
            error("Profile '%s' not found" % source)

        sys.path.append (path)
        self.resources.add (path)
        execfile(fullpath, globals())
        Profile.loaded.attach (self)

        if Profile.loaded is None:
            error('%s does not provide a valid profile (developers: ensure Profile.attach() is called.)' % source)

        if Profile.loaded.bockbuild is None:
            error ('Profile init is invalid: Failed to attach to bockbuild object')

        new_profile = Profile.loaded
        new_profile._path = fullpath
        new_profile.git_root = git_rootdir (self, os.path.dirname (path))
        config.protected_git_repos.append (new_profile.git_root)
        self.profile_name = source
        return new_profile

if __name__ == "__main__":
    try:
        global bockbuild
        bockbuild = Bockbuild()
        bockbuild.run()
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        error('%s (%s)' % (e, exc_type.__name__), more_output=True)
        error(('%s:%s @%s\t\t"%s"' % p for p in traceback.extract_tb(
            exc_traceback)[-3:]), more_output=True)
    except KeyboardInterrupt:
        error('Interrupted.')
    finally:
        finish()