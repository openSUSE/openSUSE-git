How to store the packages in git
================================

Proposals about how to store the source package information in git.


Traditional layout
------------------

With this schema we do not make any change in the current way that we
store the packages as currently done in OBS.

We will find the same compressed tarball, changes file, spec file and
maybe a set of patches.

A new .obs/ directory will be present to store some package
configuration or metadata file that will be required or used by OBS
(for example, for the Meta XML)

This layout is used in the obsgit[1] project, and can be used to
transition to a real git model.

The binaries can be stored using git-LFS[2], and there are simple
heuristics[3] to identify and track them.

Does not requires changes in the spec files and do not provide any
significant change in the workflow.  This makes it ideal as a first
step to introduce git in OBS.


Full git layout
---------------

The git repository will store the source code directly.  When possible
this will be a clone of the original upstream repository, but in other
cases it will store the uncompressed version of the upstream tarball.

From here, we have several choices that must be evaluated.  Some of
them are enumerated here.


### Single master branch

The developer will put, in the same master / main branch all the
elements required to build a package.  Mainly a traditional "spec"
file.

The "Source" tag for the spec file will be missing, but a new
directory ".obs" will be present, with a "meta.xml" file that will use
the `<scmsync>` tag to point to the master branch, as is done in the
"git-example-2"[4] project from Adrian.

The git URL for `<scmsync>` for user projects can point to external
git services, like "gitlab" or "github", but for openSUSE project
should always point to the "code.opensuse.org" repository, to avoid
history rewrite.

For example, for my home project I can have:

```xml
<scmsync>https://github.com/aplanas/package.git</scmsync>
```

Or if the user uses the openSUSE git service for their own project:

```xml
<scmsync>ssh://git@code.opensuse.org:aplanas/package.git</scmsync>
```

But for the pool of packages that will build the distribution, it
should be something like:

```xml
<scmsync>ssh://git@code.opensuse.org:pool/package.git#Tumbleweed</scmsync>
```

The hashtag in the URL is used in some package manager to specify a
branch name.


### One branch per distribution

As a linear extension of the previous layout, the developer can have
multiple branches beside master / main, one per distribution.

The master branch can contain the package layout (source + spec +
.obs/) for the personal development user fork of the package, but
there will be an "Tumbleweed", "SLE-15-SP4", "ALP-16.0" for each
project where the package lives.

In each branch we will have maybe different source codes, different
spec files and the "meta.xml" will have different `<scmsync>` URLs for
the different branches.


### One branch for the code, multiple for distribution

Another alternative is to have one single branch (master / main) for
the code, and with multiple tags to represent the different upstream
version (pristine code) or downstream versions (code that contains
specific patches for the distributions), and multiple branches to
store only the information that belong to the package (spec + .obs/).

For example, we can have the named tag "v1.0" in the main branch that
is an exact pristine representation of the upstream code, but we can
have the named tag "v1.0-openSUSE" to point to a version of the same
code that contain on top our public patches.

We will still use multiple branches per distribution to store the spec
file and the metadata that will point to the correct tag.

For example, the "meta.xml" file in the "Tumbleweed" branch will
contain this line:

```xml
<scmsync>ssh://git@code.opensuse.org:pool/package.git@v1.0-openSUSE</scmsync>
```

I am using "@" to differentiate branches from tags, but if the norm is
to use "#", should be used this instead.


### Other layouts

There are other valid source code layout, but IMHO are not suitable
for workflows that are git-friendly.

For example, the "git-example-1"[5] from Adrian is storing in the git
repository only the information about the package, delegating into OBS
the download of the pristine tarball (via #!RemoteAsset from pbuild).

This is not really suitable to work with git, as you lose the ability
to work with the source code and develop patches on top (nor those can
be migrated and shared between different code streams)

In the same way, "git-example-3"[6] is referencing the package source
using a second git instance (this time via #!RemoteAssetUrl
extension), but is again sharing the same problems that before.
Sharing code between different code streams will be very cumbersome,
as there are different git repositories involved (that represent
different concepts) that should be synchronized).

But any other layout that put together the source code the rest of
assets required to build a package, can be evaluated.


Project layout
--------------

A project should be considered as a aggregation of packages and some
metadata that set some OBS properties (like the project config).

This should resolve the mono-repo problem.  A collaboration from an
user do not require the clone of a big repository.  But also this
approach will help the release manager, as can have full control of
the distribution, using a not verify different mechanism that OBS use
today (_link file).

Git support two models to build on this concept: submodules and
subtrees.

Submodules are older, extensively documented and on the paper reflect
better the intention.  A submodule is represented as a git commit to a
different repository in the history.  It is possible to change the
commit hast that a specific submodule is pointing, and from the
release manager PoV is an explicit operation.

On the bad side submodules can be confused, and the CLI is a bit
cumbersome (initialization is multi steps in some cases and not in
others).  This should be fixed with clear documentation, and not with
helper tools (as there is a risk of complicating more the scenario).

Subtrees is a more recent alternative, more oriented to support
changes in place.  It is more clear when we are making changes in the
parent git or in one of the subtrees, and the CLI will do the right
thing for us.  This incentive a work model for the release manager
with less steps.  They can have a partial checkout of the full
project, and make in-place changes and fixes in subpackages, and when
a "git commit" is done, it will be delivered to the correct
repositories.

In any case both approach will support very similar work models, and
any difference should be fixed with documentation and a best-use
examples.

Also, in the same way a new ".osc/" directory will be added to store
the configuration files and metadata (like the project config) that
belong to OBS.


Use cases
=========

Lets describe some use cases for those layouts, and see how is
expected to work.

Should be noted that IMHO we should support the traditional layout for
a very long time, as is the less disruptive one (also the less
flexible).  Over the time (and in different stages) a full git layout
should be deployed one user demand.

TBD: present git-obs


User works with a package using the traditional layout
------------------------------------------------------

```bash
git clone ssh://git@code.opensuse.org/aplanas/package.git
```

User works with a package using the full layout
-----------------------------------------------

TBD

User add and remove personal packages
-------------------------------------

TBD

User contributes to the pool packages
-------------------------------------

TBD (reviews)

User contributes to multiple code streams
-----------------------------------------

TBD

User fix an embargoed CVE
-------------------------

TBD

Release manager updates Tumbleweed
----------------------------------

TBD


Architecture
============

TBD (OBS, git-obs, git service)


References
==========

[1] https://github.com/openSUSE/obsgit  
[2] https://git-lfs.github.com/  
[3] https://github.com/openSUSE/obsgit/blob/master/obsgit/obsgit.py#L716  
[4] https://build.opensuse.org/package/meta/home:adrianSuSE:OBSGIT/git-example-2  
[5] https://github.com/adrianschroeter/git-example-1  
[6] https://github.com/adrianschroeter/git-example-3  
