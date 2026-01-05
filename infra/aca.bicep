param name string
param location string = resourceGroup().location
param tags object = {}

param identityName string
param containerAppsEnvironmentName string
param containerRegistryName string
param serviceName string = 'aca'
param exists bool
param openAiDeploymentName string
param openAiEndpoint string
@secure()
param openAiKey string = ''

@description('Azure Storage account URL for video uploads')
param storageAccountUrl string = ''

@description('Storage container name for videos')
param storageContainerName string = 'videos'

resource acaIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

var env = [
  {
    name: 'OPENAI_HOST'
    value: 'azure'
  }
  {
    name: 'OPENAI_MODEL'
    value: openAiDeploymentName
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: openAiEndpoint
  }
  {
    name: 'RUNNING_IN_PRODUCTION'
    value: 'true'
  }
  {
    // ManagedIdentityCredential will be passed this environment variable:
    name: 'AZURE_CLIENT_ID'
    value: acaIdentity.properties.clientId
  }
]

var storageEnv = !empty(storageAccountUrl) ? [
  {
    name: 'AZURE_STORAGE_ACCOUNT_URL'
    value: storageAccountUrl
  }
  {
    name: 'AZURE_STORAGE_CONTAINER_NAME'
    value: storageContainerName
  }
  {
    name: 'VIDEO_EXTRACT_FPS'
    value: '1.0'
  }
  {
    name: 'MAX_FRAMES_PER_REQUEST'
    value: '10'
  }
] : []

var envWithSecret = !empty(openAiKey) ? union(env, storageEnv, [
  {
    name: 'AZURE_OPENAI_KEY_FOR_CHATVISION'
    secretRef: 'azure-openai-key'
  }
]) : union(env, storageEnv)

var secrets = !empty(openAiKey) ? {
  'azure-openai-key': openAiKey
} : {}

module app 'core/host/container-app-upsert.bicep' = {
  name: '${serviceName}-container-app-module'
  params: {
    name: name
    location: location
    tags: union(tags, { 'azd-service-name': serviceName })
    identityName: acaIdentity.name
    exists: exists
    containerAppsEnvironmentName: containerAppsEnvironmentName
    containerRegistryName: containerRegistryName
    env: envWithSecret
    secrets: secrets
    targetPort: 50505
  }
}

output SERVICE_ACA_IDENTITY_PRINCIPAL_ID string = acaIdentity.properties.principalId
output SERVICE_ACA_NAME string = app.outputs.name
output SERVICE_ACA_URI string = app.outputs.uri
output SERVICE_ACA_IMAGE_NAME string = app.outputs.imageName
