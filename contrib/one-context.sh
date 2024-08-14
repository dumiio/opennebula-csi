mkdir -p /var/lib/cloud
echo "${INSTANCE_ID}" > /var/lib/cloud/vm-id
chmod 755 /var/lib/cloud
chmod 644 /var/lib/cloud/vm-id