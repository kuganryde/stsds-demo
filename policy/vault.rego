package main

# Policy as code: the vault can never be provisioned weaker than the Section 35B requirement.

deny contains msg if {
	some name
	rc := input.resource.aws_s3_bucket_object_lock_configuration[name]
	rc.rule.default_retention.mode != "COMPLIANCE"
	msg := sprintf("vault %s must use COMPLIANCE retention mode", [name])
}

deny contains msg if {
	some name
	rc := input.resource.aws_s3_bucket_object_lock_configuration[name]
	rc.rule.default_retention.years < 7
	msg := sprintf("vault %s must retain for at least 7 years", [name])
}

deny contains msg if {
	some name
	b := input.resource.aws_s3_bucket[name]
	not b.object_lock_enabled
	msg := sprintf("bucket %s must enable object lock at creation", [name])
}
