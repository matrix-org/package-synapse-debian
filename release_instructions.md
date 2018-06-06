# Environment setup

You will need to have the following (non-exhaustive) packages:

    ubuntu-dev-tools git-buildpackage dh-systemd sbuild

You should create a bunch of schroots (see mk-sbuild) and add the matrix
debian repository to all the schroots.

    mk-sbuild --eatmydata jessie
    # Logout/Login to get a new session
    sudo schroot -c source:jessie-amd64 -u root # Enter the schroot
    apt-get update
    echo deb http://matrix.org/packages/debian/ jessie main > /etc/apt/sources.list.d/matrix.list
    apt-key add - <<EOF # Copy key from https://matrix.org/packages/debian/repo-key.asc
    EOF
    apt-get update
    exit # Leave the schroot
    
You will want to set ~/.gbp.conf to:

    [DEFAULT]
    builder = sbuild

to use sbuild rather than pbuilder.

    sbuild-update --keygen # Generate a signing key

# Making a release

    gbp clone git@github.com:matrix-org/package-synapse-debian
    cd package-synapse-debian
    git checkout debian
    gbp import-orig --uscan  # Scans and downloads the new source.
    gbp dch --snapshot --auto debian

New python dependencies should be added to `Build-Depends` in `debian/control`.
Packages which are not in jessie but are in jessie-backports should be added
to the matrix.org repo as per internal documentation on debian repositories.

Now try a build:

    gbp buildpackage --git-ignore-new -A -s -d jessie

If the build fails with "patch has fuzz" or something, use `quilt refresh` 
manually to refresh the patches.
  
If the build succeeds then it will have placed a .deb file in the directory
above. It is a good idea to check that is installable by copying it to a
schroot and installing it. For example:

    SESS=`schroot -b -c jessie-amd64`
    sudo cp ../matrix-synapse_0.31.0-1~1.gbp991c3a_all.deb /var/lib/schroot/mount/$SESS/
    schroot -r -c $SESS -u root -d /
    
    apt-get update
    dpkg -i /matrix-synapse_0.31.0-1~1.gbp991c3a_all.deb
    apt-get install -f
    /etc/init.d/matrix-synapse start
    
    exit
    schroot -e -c $SESS

If it works (and runs) then we can actually release it:

    gbp dch --release --auto  # Ensure that the changelog doesnt lie
    git commit -m "<RELEASE>" debian/changelog
    git clean -dfx  # This ensures that there are no uncommitted changes
    gbp buildpackage --git-tag -A -s -d jessie

To push to the repo:

    git push --all
    git push --tags
    debsign
    debrelease matrix

Finally, copy to other distributions as per internal documentation on 
debian repositories.
