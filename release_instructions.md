# Environment setup

You will need to have the following (non-exhaustive) packages:

    ubuntu-dev-tools git-buildpackage dh-systemd sbuild

You should create a bunch of schroots (see mk-sbuild) and add the matrix
debian repository to all the schroots.

    mk-sbuild --eatmydata stretch
    # Logout/Login to get a new session
    sudo schroot -c source:stretch-amd64 -u root -d / # Enter the schroot
    echo deb http://matrix.org/packages/debian/ stretch main > /etc/apt/sources.list.d/matrix.list
    apt-key add - <<EOF # Copy key from https://matrix.org/packages/debian/repo-key.asc
    EOF
    apt-get update
    exit # Leave the schroot
    
You will want to set ~/.gbp.conf to:

    [DEFAULT]
    builder = sbuild

to use sbuild rather than pbuilder.

    sbuild-update --keygen # Generate a signing key

Note: from time to time it is good to update the golden image with updates from debian. This can be done with `sbuild-update`:

    sudo sbuild-update -ugcar stretch


# Making a release

    # clone the packaging dir:
    gbp clone git@github.com:matrix-org/package-synapse-debian
    cd package-synapse-debian
    git checkout debian

```
# if reusing an existing checkout:
git clean -dfx; git checkout -- .
for b in master upstream debian; do git checkout $b; git pull; done
```
    
    # import the source
    gbp import-orig --uscan  # Scans and downloads the new source.
      # alternatively, build a tarball with:
      # ver=v0.33.3.1; git archive --format tgz --prefix="synapse-${ver}/" $ver -o synapse-$ver.tar.gz
      # then gbp import-orig path/to/synapse-$ver.tar.gz

    gbp dch --snapshot --auto

New python dependencies should be added to `Build-Depends` in `debian/control`.
Packages which are not in jessie but are in jessie-backports should be added
to the matrix.org repo as per internal documentation on debian repositories.

Now try a build:

    gbp buildpackage --git-ignore-new -A -s -d stretch

Fixing up patches:

* "patch has fuzz" normally just needs a `quilt refresh`
* conflicts need `QUILT_PATCHES=debian/patches quilt push -m -f`, fix conflicts, *then* `quilt refresh`

If the build succeeds then it will have placed a .deb file in the directory
above. It is a good idea to check that is installable by copying it to a
schroot and installing it. For example:

    v=`dpkg-parsechangelog -S Version`
    SESS=`schroot -b -c stretch-amd64`
    sudo cp ../matrix-synapse_${v}_all.deb `schroot --location -c session:$SESS`
    schroot -r -c $SESS -u root -d /
    
    debconf-set-selections <<EOF
    matrix-synapse matrix-synapse/report-stats boolean false
    matrix-synapse matrix-synapse/server-name string localhost:18448
    EOF
    
    apt-get update
    dpkg -i /matrix-synapse_*.deb
    apt-get install -f
    
    sed -i -e '/port: 8...$/{s/8448/18448/; s/8008/18008/}' -e '$aregistration_shared_secret: secret' /etc/matrix-synapse/homeserver.yaml
    /etc/init.d/matrix-synapse start
    register_new_matrix_user -c /etc/matrix-synapse/homeserver.yaml http://localhost:18008 -u test_user -p 1234  --admin
    
    #...
    
    /etc/init.d/matrix-synapse stop
    exit
    schroot -e -c $SESS

If it works (and runs) then we can actually release it:

    # add -U high|low|emergency|etc to the following for urgency
    # https://www.debian.org/doc/debian-policy/ch-controlfields.html#urgency
    #
    # NB! set the version to 0.<X>.<Y>-1matrix1 to distinguish our packages from
    # the official debian ones.
    gbp dch --auto --release --force-distribution -D stretch
    
    git commit -m "`dpkg-parsechangelog -S Version`" debian/changelog
    git clean -dfx  # This ensures that there are no uncommitted changes
    git checkout -- .
    gbp buildpackage --git-tag -A -s -d stretch

To push to the repo:

    git push --all
    git push --tags
    debsign
    debrelease matrix

Finally, copy to other distributions:

    # in the repo directory on the packages server:
    for i in buster sid xenial bionic cosmic; do reprepro -V copysrc $i stretch matrix-synapse; done
    # ensure the versions are expected for all the distributions:
    reprepro ls matrix-synapse
